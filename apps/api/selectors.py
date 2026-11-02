"""Read-side helpers that assemble the rich per-day attendance shape for one
employee. All access is scoped to the passed-in employee.

Full-time days carry AM/PM sessions (in/out, status, exact minutes_late) plus
authorized overtime minutes; part-time days carry a single in/out, the
minutes worked and an incomplete flag (part-timers are judged per week - see
``weekly_hours``). Every day also carries a ``day_status``
(full-time PRESENT / LATE / HALF_DAY / ABSENT; part-time PRESENT / INCOMPLETE;
PENDING while today's time out hasn't been reached).
"""

from __future__ import annotations

import calendar
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.attendance.models import Absence, AttendanceLog, Lates, Overtime, Undertime
from apps.attendance.services import (
    event_requirement_met,
    holiday_requires_attendance,
    session_is_open,
)
from apps.attendance.tracking import tracking_start
from apps.organization.models import Holiday
from apps.organization.schedule import get_effective_schedule
from apps.organization.trusted_time import trusted_localdate

DAY_PRESENT = 'PRESENT'
DAY_LATE = 'LATE'
DAY_HALF = 'HALF_DAY'
DAY_ABSENT = 'ABSENT'
DAY_PENDING = 'PENDING'   # today, time out not reached yet - not judged
DAY_INCOMPLETE = 'INCOMPLETE'   # part-time: one of IN/OUT missing -> 0 hours


def _reduce_logs(logs):
    """{date: {log_type: datetime}} — earliest for *_IN, latest for *_OUT."""
    by_date: dict[date_cls, dict[str, datetime]] = {}
    for log in logs:
        d = timezone.localtime(log.log_datetime).date()
        slot = by_date.setdefault(d, {})
        t = log.log_type
        if t not in slot:
            slot[t] = log.log_datetime
        elif t.endswith('_OUT') or t == 'OUT':
            slot[t] = log.log_datetime  # keep latest
    return by_date


def build_daily_attendance(employee, start: date_cls, end: date_cls) -> list[dict]:
    sched = get_effective_schedule(employee)
    today = trusted_localdate()

    day_start = timezone.make_aware(datetime.combine(start, time.min))
    day_end = timezone.make_aware(datetime.combine(end, time.min)) + timedelta(days=1)

    logs_by_date = _reduce_logs(
        AttendanceLog.objects.filter(
            employee=employee, log_datetime__gte=day_start, log_datetime__lt=day_end,
        ).order_by('log_datetime'))

    lates = {
        (r['date'], r['session']): r['minutes_late']
        for r in Lates.objects.filter(employee=employee, date__gte=start, date__lte=end)
        .values('date', 'session', 'minutes_late')
    }
    undertimes = {
        (r['date'], r['session']): r['minutes_undertime']
        for r in Undertime.objects.filter(employee=employee, date__gte=start, date__lte=end)
        .values('date', 'session', 'minutes_undertime')
    }
    absences = {
        (a.date, a.session): a
        for a in Absence.objects.filter(employee=employee, date__gte=start, date__lte=end)
    }
    overtime = {
        o.date: o.minutes
        for o in Overtime.objects.filter(employee=employee, date__gte=start, date__lte=end)
    }
    holidays = {h.date: h for h in Holiday.objects.filter(date__gte=start, date__lte=end)}

    # A workday with no data at all is only *inferred* absent once tracking had
    # begun — before that nobody was being recorded, so there is nothing to say.
    # (Days with real punches/rows are always shown, whatever the start date.)
    first_tracked = tracking_start(employee)

    records = []
    day = start
    while day <= end:
        is_workday = day.weekday() in sched.workdays
        holiday = holidays.get(day)
        event_sessions = holiday.required_sessions if holiday else ()
        day_logs = logs_by_date.get(day, {})
        has_data = bool(day_logs) or any(k[0] == day for k in absences) or any(
            k[0] == day for k in lates)

        # A holiday normally needs no punch (and isn't shown at all, like a
        # weekend) - unless the employee bridged an unauthorized absence onto
        # it (see process_day / holiday_requires_attendance), in which case it
        # counts as an ordinary workday, punches or not. An event day (special
        # holiday, attendance required) always counts.
        if event_sessions:
            counts_as_workday = True
        elif holiday and not has_data:
            counts_as_workday = holiday_requires_attendance(employee, sched, day, today=today)
        else:
            counts_as_workday = holiday is None

        if employee.is_fulltime:
            include = has_data or (
                is_workday and counts_as_workday and first_tracked <= day <= today)
        else:
            # Part-timers choose their days; only days they came in exist.
            include = bool(day_logs)
        if include:
            if employee.is_fulltime:
                rec = _fulltime_record(day, day_logs, lates, undertimes, overtime, sched)
                if event_requirement_met(day_logs, event_sessions):
                    rec = _event_credit(rec, holiday)
                records.append(rec)
            else:
                records.append(_parttime_record(day, day_logs, sched))
        day += timedelta(days=1)

    return records


def _fulltime_record(day, day_logs, lates, undertimes, overtime, sched) -> dict:
    def session(prefix, name, sched_out):
        in_dt = day_logs.get(f'{prefix}_IN')
        out_dt = day_logs.get(f'{prefix}_OUT')
        minutes = lates.get((day, name), 0)
        under = undertimes.get((day, name), 0)
        # Same rule process_day applies: a session counts only with BOTH punches.
        # Decided from the punches themselves — never from the mere *absence* of
        # an Absence row, which is missing for any day the daily job never ran
        # (that used to make a day with no punches at all read as PRESENT).
        if in_dt is None or out_dt is None:
            # ...but not before the session's scheduled time out has been reached.
            status = DAY_PENDING if session_is_open(day, sched_out) else DAY_ABSENT
        elif minutes > 0:
            status = DAY_LATE
        else:
            status = DAY_PRESENT
        # A missed session loses its whole scheduled length.
        absent = sched.session_minutes(name) if status == DAY_ABSENT else 0
        return {'in': in_dt, 'out': out_dt, 'status': status,
                'minutes_late': minutes, 'minutes_undertime': under,
                'minutes_absent': absent}

    am = session('AM', 'AM', sched.am_out)
    pm = session('PM', 'PM', sched.pm_out)
    present = sum(1 for s in (am, pm) if s['status'] in (DAY_PRESENT, DAY_LATE))
    any_late = am['status'] == DAY_LATE or pm['status'] == DAY_LATE

    if DAY_PENDING in (am['status'], pm['status']):
        day_status = DAY_PENDING
    elif present == 2:
        day_status = DAY_LATE if any_late else DAY_PRESENT
    elif present == 1:
        day_status = DAY_HALF
    else:
        day_status = DAY_ABSENT

    late_minutes = am['minutes_late'] + pm['minutes_late']
    undertime_minutes = am['minutes_undertime'] + pm['minutes_undertime']
    absent_minutes = am['minutes_absent'] + pm['minutes_absent']

    return {
        'date': day, 'employee_type': 'FULL_TIME',
        'am': am, 'pm': pm,
        'overtime_minutes': overtime.get(day, 0),
        'late_minutes': late_minutes,
        'undertime_minutes': undertime_minutes,
        'absent_minutes': absent_minutes,
        'lost_minutes': late_minutes + undertime_minutes + absent_minutes,
        'day_status': day_status,
    }


def _event_credit(rec, holiday) -> dict:
    """The required session(s) of an event day were attended: the whole day
    counts as present - nothing late, undertime or absent (process_day's rule)."""
    for s in ('am', 'pm'):
        rec[s].update(status=DAY_PRESENT, minutes_late=0, minutes_undertime=0,
                      minutes_absent=0)
    rec.update(day_status=DAY_PRESENT, late_minutes=0, undertime_minutes=0,
               absent_minutes=0, lost_minutes=0,
               event=f'{holiday.name} ({holiday.required_label} required)')
    return rec


def _parttime_record(day, day_logs, sched) -> dict:
    """A day a part-timer came in. Hours rendered = OUT - IN (no break
    deducted), and only with both punches (process_day's rule): one punch =>
    INCOMPLETE, 0 hours, flagged with the missing side. They're judged per
    week (``weekly_hours``), so a day is never ABSENT and carries no lost time.
    """
    in_dt = day_logs.get('IN')
    out_dt = day_logs.get('OUT')
    record = {'date': day, 'employee_type': 'PART_TIME', 'in': in_dt, 'out': out_dt,
              'incomplete': False, 'missing': None, 'worked_minutes': 0,
              'absent_minutes': 0, 'lost_minutes': 0}
    if in_dt is not None and out_dt is not None:
        record['day_status'] = DAY_PRESENT
        record['worked_minutes'] = max(0, int((out_dt - in_dt).total_seconds() // 60))
    elif session_is_open(day, sched.pm_out):
        record['day_status'] = DAY_PENDING
    else:
        record['day_status'] = DAY_INCOMPLETE
        record['incomplete'] = True
        record['missing'] = 'OUT' if in_dt is not None else 'IN' if out_dt is not None else None
    return record


WEEK_MET = 'MET'
WEEK_SHORT = 'SHORT'
WEEK_IN_PROGRESS = 'IN_PROGRESS'   # week not over and not yet met


def weekly_hours(employee, start: date_cls, end: date_cls, today=None) -> list[dict]:
    """A part-timer's weeks (Mon-Sun) overlapping [start, end], oldest first:
    hours rendered vs. their weekly requirement. A week is MET only when the
    hours are reached AND they came in on enough different days
    (``weekly_min_days``; only complete IN+OUT days count).

    Each week is counted whole, even where it spills past the range, since
    the requirement is per week. Weeks before tracking began for them, or
    that haven't started yet, are left out. A week that's still running
    reads IN_PROGRESS until it's met - it can't be SHORT before it's over.
    """
    sched = get_effective_schedule(employee)
    today = today or trusted_localdate()
    first = start - timedelta(days=start.weekday())
    last = end + timedelta(days=6 - end.weekday())

    worked: dict[date_cls, int] = {}
    days: dict[date_cls, int] = {}
    for rec in build_daily_attendance(employee, first, last):
        week = rec['date'] - timedelta(days=rec['date'].weekday())
        worked[week] = worked.get(week, 0) + rec.get('worked_minutes', 0)
        if rec.get('worked_minutes'):
            days[week] = days.get(week, 0) + 1

    required = sched.weekly_minutes
    required_days = sched.weekly_min_days
    tracked_from = tracking_start(employee)
    weeks = []
    week = first
    while week <= end and week <= today:
        week_end = week + timedelta(days=6)
        if week_end >= tracked_from:
            done = worked.get(week, 0)
            days_worked = days.get(week, 0)
            # Both: the hours, AND spread over enough different days.
            if done >= required and days_worked >= required_days:
                status = WEEK_MET
            elif week_end >= today:
                status = WEEK_IN_PROGRESS
            else:
                status = WEEK_SHORT
            weeks.append({'week_start': week, 'week_end': week_end,
                          'worked_minutes': done, 'required_minutes': required,
                          'remaining_minutes': max(0, required - done),
                          'days_worked': days_worked, 'required_days': required_days,
                          'remaining_days': max(0, required_days - days_worked),
                          'status': status})
        week += timedelta(days=7)
    return weeks


def monthly_summary(employee, year: int, month: int) -> dict:
    first = date_cls(year, month, 1)
    last = date_cls(year, month, calendar.monthrange(year, month)[1])
    records = build_daily_attendance(employee, first, last)

    def count(status):
        return sum(1 for r in records if r['day_status'] == status)

    overtime_minutes = sum(r.get('overtime_minutes', 0) for r in records)
    late_minutes = sum(r.get('late_minutes', 0) for r in records)
    undertime_minutes = sum(r.get('undertime_minutes', 0) for r in records)
    absent_minutes = sum(r.get('absent_minutes', 0) for r in records)

    return {
        'month': f'{year:04d}-{month:02d}',
        'present': count(DAY_PRESENT),
        'late': count(DAY_LATE),
        'half_day': count(DAY_HALF),
        'absent': count(DAY_ABSENT),
        'incomplete': count(DAY_INCOMPLETE),
        'worked_minutes': sum(r.get('worked_minutes', 0) for r in records),   # part-time
        'overtime_minutes': overtime_minutes,
        'late_minutes': late_minutes,
        'undertime_minutes': undertime_minutes,
        'absent_minutes': absent_minutes,
        'lost_minutes': late_minutes + undertime_minutes + absent_minutes,
    }
