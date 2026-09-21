"""Read-side helpers that assemble the rich per-day attendance shape for one
employee. All access is scoped to the passed-in employee.

Full-time days carry AM/PM sessions (in/out, status, exact minutes_late) plus
authorized overtime minutes; part-time days carry a single in/out with an
incomplete flag. Every day also carries a ``day_status``
(PRESENT / LATE / HALF_DAY / ABSENT).
"""

from __future__ import annotations

import calendar
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.attendance.models import Absence, AttendanceLog, Lates, Overtime, Undertime
from apps.attendance.tracking import tracking_start
from apps.organization.models import Holiday
from apps.organization.schedule import get_effective_schedule

DAY_PRESENT = 'PRESENT'
DAY_LATE = 'LATE'
DAY_HALF = 'HALF_DAY'
DAY_ABSENT = 'ABSENT'


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
    today = timezone.localdate()

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
    holidays = set(
        Holiday.objects.filter(date__gte=start, date__lte=end).values_list('date', flat=True))

    # A workday with no data at all is only *inferred* absent once tracking had
    # begun — before that nobody was being recorded, so there is nothing to say.
    # (Days with real punches/rows are always shown, whatever the start date.)
    first_tracked = tracking_start(employee)

    records = []
    day = start
    while day <= end:
        is_workday = day.weekday() in sched.workdays
        is_holiday = day in holidays
        day_logs = logs_by_date.get(day, {})
        has_data = bool(day_logs) or any(k[0] == day for k in absences) or any(
            k[0] == day for k in lates)

        include = has_data or (
            is_workday and not is_holiday and first_tracked <= day <= today)
        if include and not is_holiday:
            if employee.is_fulltime:
                records.append(_fulltime_record(
                    day, day_logs, lates, undertimes, overtime))
            else:
                records.append(_parttime_record(day, day_logs))
        day += timedelta(days=1)

    return records


def _fulltime_record(day, day_logs, lates, undertimes, overtime) -> dict:
    def session(prefix, name):
        in_dt = day_logs.get(f'{prefix}_IN')
        out_dt = day_logs.get(f'{prefix}_OUT')
        minutes = lates.get((day, name), 0)
        under = undertimes.get((day, name), 0)
        # Same rule process_day applies: a session counts only with BOTH punches.
        # Decided from the punches themselves — never from the mere *absence* of
        # an Absence row, which is missing for any day the daily job never ran
        # (that used to make a day with no punches at all read as PRESENT).
        if in_dt is None or out_dt is None:
            status = DAY_ABSENT
        elif minutes > 0:
            status = DAY_LATE
        else:
            status = DAY_PRESENT
        return {'in': in_dt, 'out': out_dt, 'status': status,
                'minutes_late': minutes, 'minutes_undertime': under}

    am = session('AM', 'AM')
    pm = session('PM', 'PM')
    present = sum(1 for s in (am, pm) if s['status'] != DAY_ABSENT)
    any_late = am['status'] == DAY_LATE or pm['status'] == DAY_LATE

    if present == 2:
        day_status = DAY_LATE if any_late else DAY_PRESENT
    elif present == 1:
        day_status = DAY_HALF
    else:
        day_status = DAY_ABSENT

    late_minutes = am['minutes_late'] + pm['minutes_late']
    undertime_minutes = am['minutes_undertime'] + pm['minutes_undertime']

    return {
        'date': day, 'employee_type': 'FULL_TIME',
        'am': am, 'pm': pm,
        'overtime_minutes': overtime.get(day, 0),
        'late_minutes': late_minutes,
        'undertime_minutes': undertime_minutes,
        'lost_minutes': late_minutes + undertime_minutes,
        'day_status': day_status,
    }


def _parttime_record(day, day_logs) -> dict:
    """Present only with both IN and OUT (process_day's rule), decided from the
    punches: one punch => incomplete (missing the other side), none => plain
    absent."""
    in_dt = day_logs.get('IN')
    out_dt = day_logs.get('OUT')
    if in_dt is not None and out_dt is not None:
        return {
            'date': day, 'employee_type': 'PART_TIME',
            'in': in_dt, 'out': out_dt, 'day_status': DAY_PRESENT,
            'incomplete': False, 'missing': None,
        }
    missing = 'OUT' if in_dt is not None else 'IN' if out_dt is not None else None
    return {
        'date': day, 'employee_type': 'PART_TIME',
        'in': in_dt, 'out': out_dt, 'day_status': DAY_ABSENT,
        'incomplete': missing is not None, 'missing': missing,
    }


def monthly_summary(employee, year: int, month: int) -> dict:
    first = date_cls(year, month, 1)
    last = date_cls(year, month, calendar.monthrange(year, month)[1])
    records = build_daily_attendance(employee, first, last)

    def count(status):
        return sum(1 for r in records if r['day_status'] == status)

    overtime_minutes = sum(r.get('overtime_minutes', 0) for r in records)
    late_minutes = sum(r.get('late_minutes', 0) for r in records)
    undertime_minutes = sum(r.get('undertime_minutes', 0) for r in records)

    return {
        'month': f'{year:04d}-{month:02d}',
        'present': count(DAY_PRESENT),
        'late': count(DAY_LATE),
        'half_day': count(DAY_HALF),
        'absent': count(DAY_ABSENT),
        'overtime_minutes': overtime_minutes,
        'late_minutes': late_minutes,
        'undertime_minutes': undertime_minutes,
        'lost_minutes': late_minutes + undertime_minutes,
    }
