"""Attendance ingestion & processing (Phase 8).

Pure, unit-testable helpers do the punch→session assignment and the minute math;
the orchestration (``sync_attendance``, ``process_day``) wires them into the ORM.

Rules (locked):
  * Full-time: AM_IN/AM_OUT/PM_IN/PM_OUT; per-session tardiness (exact minutes)
    vs admin AM/PM in + grace; per-session absence (half-day possible).
  * Overtime is a privilege: computed ONLY when an OTAuthorization exists, from
    OT_IN/OT_OUT within the authorized window. No auto/threshold OT.
  * Part-time: IN/OUT only, no tardiness/overtime; missing one punch => absent +
    incomplete (with the correct missing side); no punch => plain absent.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.devices.clients import RawPunch, get_device_client
from apps.organization.models import Employee, Holiday
from apps.organization.schedule import get_effective_schedule

from .models import (
    Absence,
    AttendanceLog,
    Lates,
    OTAuthorization,
    Overtime,
    Session,
    Undertime,
)

logger = logging.getLogger('apps.attendance')

# Day-status values returned by process_day / used by the API + portal.
STATUS_PRESENT = 'PRESENT'
STATUS_LATE = 'LATE'
STATUS_HALF_DAY = 'HALF_DAY'
STATUS_ABSENT = 'ABSENT'
STATUS_REST = 'REST'       # non-workday / holiday — nothing derived


# ---------------------------------------------------------------------------
# Pure helpers (no DB)
# ---------------------------------------------------------------------------
def minutes_late(actual_in: time, expected_in: time, grace_minutes: int) -> int:
    """Exact minutes an IN punch is past ``expected_in + grace``; 0 within grace."""
    actual = actual_in.hour * 60 + actual_in.minute
    threshold = expected_in.hour * 60 + expected_in.minute + grace_minutes
    return max(0, actual - threshold)


def minutes_undertime(actual_out: time, expected_out: time) -> int:
    """Exact minutes an OUT punch is BEFORE ``expected_out``; 0 if at/after.

    No grace is applied — the grace period forgives late *arrivals* only.
    """
    actual = actual_out.hour * 60 + actual_out.minute
    expected = expected_out.hour * 60 + expected_out.minute
    return max(0, expected - actual)


def _group_in_out(sorted_dts, in_type, out_type, sched_out: time | None = None) -> dict:
    """earliest => in_type; latest (if >1) => out_type.

    ``sched_out``, when given, blocks a punch at/after that time from ever
    becoming the IN: a session's whole point is that IN happens before
    quitting time. ``sorted_dts`` is chronological within one calendar day, so
    if the *earliest* punch is already at/after ``sched_out``, every punch in
    the bucket is — there is no valid IN at all, and the bucket is filed as a
    lone OUT (the latest of them) instead of an IN with no matching OUT.
    Without this, a single stray after-hours punch reads as "showed up, never
    left"; two of them read as "present, just very late" — both wrong, since
    nobody was actually there during the session.
    """
    if not sorted_dts:
        return {}
    if sched_out is not None and timezone.localtime(sorted_dts[0]).time() >= sched_out:
        return {out_type: sorted_dts[-1]}
    result = {in_type: sorted_dts[0]}
    if len(sorted_dts) > 1:
        result[out_type] = sorted_dts[-1]
    return result


def assign_fulltime_punches(times, midpoint: time, ot_start: time | None = None,
                             am_out: time | None = None, pm_out: time | None = None) -> dict:
    """Classify a full-time employee's punches for one day into log types.

    AM = at/before midpoint. If ``ot_start`` (authorization) is given, PM = after
    midpoint and before ot_start, OT = at/after ot_start. Without authorization
    there is no OT window (late punches simply extend PM_OUT — unless every AM
    or PM punch is at/after that session's ``am_out``/``pm_out``, in which case
    none of them qualify as an IN; see ``_group_in_out``).
    """
    ordered = sorted(times)
    am, pm, ot = [], [], []
    for dt in ordered:
        t = timezone.localtime(dt).time()
        if t <= midpoint:
            am.append(dt)
        elif ot_start is not None and t >= ot_start:
            ot.append(dt)
        else:
            pm.append(dt)

    result = {}
    result.update(_group_in_out(am, 'AM_IN', 'AM_OUT', am_out))
    result.update(_group_in_out(pm, 'PM_IN', 'PM_OUT', pm_out))
    if ot_start is not None:
        result.update(_group_in_out(ot, 'OT_IN', 'OT_OUT'))
    return result


def assign_parttime_punches(times) -> dict:
    """earliest => IN; latest => OUT (a single punch is the IN)."""
    return _group_in_out(sorted(times), 'IN', 'OUT')


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def sync_attendance(device=None, since: datetime | None = None) -> dict:
    """Pull punches via the configured backend and ingest them (classify, store,
    fire immediate per-punch notifications, recompute days)."""
    client = get_device_client()
    raw_punches: list[RawPunch] = client.fetch_attendance(since)

    summary = ingest_punches(raw_punches, device=device)

    if device is not None:
        device.last_synced_at = timezone.now()
        device.save(update_fields=['last_synced_at'])

    logger.info('sync_attendance summary: %s', summary)
    return summary


def _store_log(employee_id, device, log_dt, log_type, source=None, created_by=None):
    """Insert one AttendanceLog respecting the unique constraint.
    Returns (log, was_created); (None, False) on a losing insert race."""
    try:
        with transaction.atomic():
            log, was_created = AttendanceLog.objects.get_or_create(
                employee_id=employee_id,
                log_datetime=log_dt,
                log_type=log_type,
                defaults={
                    'device': device,
                    'source': source or AttendanceLog.Source.DEVICE,
                    'created_by': created_by,
                },
            )
        return log, was_created
    except IntegrityError:
        return None, False


def _notify_for_new_log(log):
    """Fire the immediate per-punch notification (Phase 10). Isolated so a
    notification hiccup never breaks attendance ingestion."""
    if log is None:
        return
    try:
        from apps.notifications.services import notify_for_log
        notify_for_log(log)
    except Exception:
        logger.exception('notify_for_log failed for log %s', getattr(log, 'id', '?'))


def ingest_punches(raw_punches, device=None, source=None, notify=True) -> dict:
    """Classify + store a batch of raw punches (with per-day context so a single
    late-arriving punch is still classified correctly), firing an immediate
    notification for each NEW log, then recompute affected days.

    Shared by ``sync_attendance`` and the real-time ``attendance_listener``.

    ``source`` tags the stored logs (default: a real device punch) and
    ``notify=False`` skips the per-punch notification / push / live-page signal.
    Both exist for ``seed_demo_attendance``: fake punches must be classified by
    the exact production rules, but must never push notifications to real phones.
    """
    by_biometric = {e.biometric_id: e for e in Employee.objects.filter(is_active=True)}
    grouped: dict[int, dict[date_cls, list]] = defaultdict(lambda: defaultdict(list))
    emp_by_id: dict[int, Employee] = {}
    skipped = 0
    for punch in raw_punches:
        employee = by_biometric.get(str(punch.biometric_id))
        if employee is None:
            skipped += 1
            logger.warning('Unknown biometric_id %s — punch skipped', punch.biometric_id)
            continue
        emp_by_id[employee.id] = employee
        grouped[employee.id][timezone.localtime(punch.timestamp).date()].append(punch.timestamp)

    created = duplicates = 0
    affected: set[tuple[int, date_cls]] = set()

    for emp_id, per_date in grouped.items():
        employee = emp_by_id[emp_id]
        sched = get_effective_schedule(employee)
        for day, times in per_date.items():
            all_times = sorted(set(times) | _existing_log_times(employee, day))
            if employee.is_fulltime:
                auth = OTAuthorization.objects.filter(employee=employee, date=day).first()
                assigned = assign_fulltime_punches(
                    all_times, sched.midpoint, auth.ot_start if auth else None,
                    am_out=sched.am_out, pm_out=sched.pm_out)
            else:
                assigned = assign_parttime_punches(all_times)

            for log_type, log_dt in assigned.items():
                log, was_created = _store_log(emp_id, device, log_dt, log_type, source=source)
                if was_created:
                    created += 1
                    if notify:
                        _notify_for_new_log(log)
                elif log is not None:
                    duplicates += 1
            affected.add((emp_id, day))

    lates = undertimes = absences = ot_records = 0
    for emp_id, day in sorted(affected, key=lambda x: (x[0], x[1])):
        result = process_day(emp_by_id[emp_id], day)
        lates += result['lates']
        undertimes += result['undertimes']
        absences += result['absences']
        ot_records += result['overtime']

    return {
        'created': created, 'skipped': skipped, 'duplicates': duplicates,
        'dates_processed': len({d for _, d in affected}),
        'lates': lates, 'undertimes': undertimes,
        'absences': absences, 'overtime': ot_records,
    }


def _existing_log_times(employee, target_date: date_cls) -> set:
    """The datetimes of already-stored punches for a day (so a single new punch
    is classified against the full day's context)."""
    day_start = timezone.make_aware(datetime.combine(target_date, time.min))
    day_end = day_start + timedelta(days=1)
    return set(
        AttendanceLog.objects.filter(
            employee=employee, log_datetime__gte=day_start, log_datetime__lt=day_end,
        ).values_list('log_datetime', flat=True)
    )


# ---------------------------------------------------------------------------
# Per-day processing (idempotent)
# ---------------------------------------------------------------------------
def _day_logs(employee, target_date: date_cls) -> dict:
    """Return {log_type: AttendanceLog} for one local day. For duplicate types,
    keep earliest for *_IN and latest for *_OUT."""
    day_start = timezone.make_aware(datetime.combine(target_date, time.min))
    day_end = day_start + timedelta(days=1)
    logs = AttendanceLog.objects.filter(
        employee=employee, log_datetime__gte=day_start, log_datetime__lt=day_end,
    ).order_by('log_datetime')

    picked: dict[str, AttendanceLog] = {}
    for log in logs:
        t = log.log_type
        if t not in picked:
            picked[t] = log
        elif t.endswith('_OUT') or t == 'OUT':
            picked[t] = log  # keep latest
        # *_IN / IN: keep earliest (already first due to ordering)
    return picked


@transaction.atomic
def process_day(employee, target_date: date_cls) -> dict:
    """Recompute all derived rows (Lates/Absence/Overtime) for one (employee,
    date). Idempotent: clears the day first, then recomputes."""
    sched = get_effective_schedule(employee)

    Lates.objects.filter(employee=employee, date=target_date).delete()
    Undertime.objects.filter(employee=employee, date=target_date).delete()
    Absence.objects.filter(employee=employee, date=target_date).delete()
    Overtime.objects.filter(employee=employee, date=target_date).delete()

    result = {'status': STATUS_REST, 'lates': 0, 'undertimes': 0,
              'absences': 0, 'overtime': 0}

    if target_date.weekday() not in sched.workdays:
        return result
    if Holiday.objects.filter(date=target_date).exists():
        return result

    logs = _day_logs(employee, target_date)
    if employee.is_fulltime:
        return _process_fulltime(employee, target_date, sched, logs)
    return _process_parttime(employee, target_date, logs)


def _process_fulltime(employee, target_date, sched, logs) -> dict:
    lates = undertimes = absences = 0
    sessions_present = 0
    late_flag = False

    for session, in_type, out_type, sched_in, sched_out in (
        (Session.AM, 'AM_IN', 'AM_OUT', sched.am_in, sched.am_out),
        (Session.PM, 'PM_IN', 'PM_OUT', sched.pm_in, sched.pm_out),
    ):
        in_log = logs.get(in_type)
        out_log = logs.get(out_type)

        if in_log and out_log:
            sessions_present += 1
            local_in = timezone.localtime(in_log.log_datetime).time()
            late = minutes_late(local_in, sched_in, sched.grace_period_minutes)
            if late > 0:
                Lates.objects.create(
                    employee=employee, date=target_date, session=session,
                    attendance_log=in_log, minutes_late=late)
                lates += 1
                late_flag = True

            # Undertime: left before the session's scheduled OUT (no grace).
            local_out = timezone.localtime(out_log.log_datetime).time()
            under = minutes_undertime(local_out, sched_out)
            if under > 0:
                Undertime.objects.create(
                    employee=employee, date=target_date, session=session,
                    attendance_log=out_log, minutes_undertime=under)
                undertimes += 1
        else:
            # Missing session -> absence (half-day if the other session is present).
            if in_log and not out_log:
                reason, incomplete = Absence.Reason.MISSING_OUT, True
            elif out_log and not in_log:
                reason, incomplete = Absence.Reason.MISSING_IN, True
            else:
                reason, incomplete = Absence.Reason.NO_PUNCH, False
            Absence.objects.create(
                employee=employee, date=target_date, session=session,
                reason=reason, incomplete=incomplete)
            absences += 1

    if sessions_present == 2:
        status = STATUS_LATE if late_flag else STATUS_PRESENT
    elif sessions_present == 1:
        status = STATUS_HALF_DAY
    else:
        status = STATUS_ABSENT

    overtime = _compute_overtime(employee, target_date, logs)

    return {'status': status, 'lates': lates, 'undertimes': undertimes,
            'absences': absences, 'overtime': overtime}


def _compute_overtime(employee, target_date, logs) -> int:
    """Only if an OTAuthorization exists. Returns 1 if an Overtime row was made."""
    auth = OTAuthorization.objects.filter(employee=employee, date=target_date).first()
    if auth is None:
        return 0

    ot_in = logs.get('OT_IN')
    ot_out = logs.get('OT_OUT')
    if not (ot_in and ot_out):
        return 0  # authorized but no OT punches => 0 OT (no record)

    ot_in_local = timezone.localtime(ot_in.log_datetime)
    ot_out_local = timezone.localtime(ot_out.log_datetime)
    ot_start_dt = timezone.make_aware(datetime.combine(target_date, auth.ot_start))
    effective_start = max(ot_in_local, ot_start_dt)
    minutes = max(0, int((ot_out_local - effective_start).total_seconds() // 60))

    Overtime.objects.create(
        employee=employee, date=target_date, authorization=auth,
        ot_in_log=ot_in, ot_out_log=ot_out, minutes=minutes)
    return 1


def _process_parttime(employee, target_date, logs) -> dict:
    in_log = logs.get('IN')
    out_log = logs.get('OUT')

    if in_log and out_log:
        return {'status': STATUS_PRESENT, 'lates': 0, 'undertimes': 0,
                'absences': 0, 'overtime': 0}

    if in_log or out_log:
        reason = Absence.Reason.MISSING_OUT if in_log else Absence.Reason.MISSING_IN
        Absence.objects.create(
            employee=employee, date=target_date, session=Session.FULL,
            reason=reason, incomplete=True)
    else:
        Absence.objects.create(
            employee=employee, date=target_date, session=Session.FULL,
            reason=Absence.Reason.NO_PUNCH, incomplete=False)

    return {'status': STATUS_ABSENT, 'lates': 0, 'undertimes': 0,
            'absences': 1, 'overtime': 0}
