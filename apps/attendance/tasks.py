"""Celery tasks for attendance ingestion and daily processing."""

from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta

from celery import shared_task

from apps.organization.models import Employee, Holiday
from apps.organization.schedule import get_effective_schedule
from apps.organization.trusted_time import refresh_offset, trusted_localdate, trusted_now

from .models import Absence, ProcessingCursor
from .services import process_day, sync_attendance
from .tracking import employee_floor, system_start

logger = logging.getLogger('apps.attendance')


@shared_task(name='attendance.sync_attendance')
def sync_attendance_task():
    """Pull punches from the configured backend (called by Celery Beat), then
    process today's derived rows + notifications."""
    from apps.devices.models import BiometricDevice

    try:
        refresh_offset()  # piggyback the trusted-time check on this interval
    except Exception:
        logger.exception('Trusted-time refresh failed; keeping the last known offset.')

    device = BiometricDevice.objects.filter(is_active=True).first()
    since = trusted_now() - timedelta(days=1)
    summary = sync_attendance(device=device, since=since)

    try:
        catch_up_finalized_days()
    except Exception:
        logger.exception('Catch-up of missed days failed; will retry next sync.')
    process_daily_attendance_task(trusted_localdate().isoformat())
    logger.info('sync_attendance_task done: %s', summary)
    return summary


def _coerce_date(value) -> date_cls:
    if isinstance(value, date_cls):
        return value
    return datetime.strptime(value, '%Y-%m-%d').date()


# How far back to recheck a holiday's bridging status. A holiday's own
# processing can only know about the workday *after* it once that day has
# actually happened, so each day's run re-settles any recent holiday that was
# left provisionally exempt pending that information.
HOLIDAY_REEVAL_DAYS = 7


def _process_and_notify(emp, sched, target, raise_attendance_notification):
    """process_day for one (employee, date) + any resulting ABSENCE
    notifications. Returns (lates, absences, notified); (0, 0, 0) on a
    non-workday for this employee (nothing to do)."""
    if target.weekday() not in sched.workdays:
        return 0, 0, 0

    result = process_day(emp, target)
    absences = notified = 0
    for ab in Absence.objects.filter(employee=emp, date=target):
        absences += 1
        label = 'ABSENT' if not ab.incomplete else f'ABSENT ({ab.get_reason_display()})'
        scope = '' if ab.session == 'FULL' else f' [{ab.session}]'
        if raise_attendance_notification(
            emp, 'ABSENCE', title='Marked Absent',
            body=f'You were marked {label}{scope} on {target:%Y-%m-%d}.'):
            notified += 1
    return result['lates'], absences, notified


def catch_up_finalized_days(today=None) -> dict:
    """Process every past day not yet finalized - i.e. any day the stack was
    down for - through yesterday, then advance the cursor.

    Only *yesterday* may raise "Marked Absent" pushes (idempotent, so a day
    the live job already notified isn't notified twice); older days are filled
    in silently, like ``backfill_attendance`` - a push about a day long gone is
    noise. Starts from when tracking began the first time it ever runs.
    """
    from apps.notifications.tasks import raise_attendance_notification

    today = today or trusted_localdate()
    yesterday = today - timedelta(days=1)
    cursor = ProcessingCursor.load()
    day = (cursor.last_finalized_date + timedelta(days=1)
           if cursor.last_finalized_date else system_start())
    if day > yesterday:
        return {'days': 0}

    employees = [(emp, get_effective_schedule(emp), employee_floor(emp)) for emp in
                 Employee.objects.filter(is_active=True).select_related('schedule_override')]
    days = absences = 0
    while day <= yesterday:
        for emp, sched, floor in employees:
            if day < floor:
                continue   # before they were in the system / hired
            if day == yesterday:
                absences += _process_and_notify(
                    emp, sched, day, raise_attendance_notification)[1]
            else:
                absences += process_day(emp, day)['absences']
        # Advance per day so a long catch-up interrupted midway resumes, not restarts.
        cursor.last_finalized_date = day
        cursor.save(update_fields=['last_finalized_date', 'updated_at'])
        days += 1
        day += timedelta(days=1)

    logger.info('catch_up_finalized_days: %s day(s), %s absence(s)', days, absences)
    return {'days': days, 'absences': absences}


@shared_task(name='attendance.process_daily_attendance')
def process_daily_attendance_task(date):
    """Recompute one date for every active employee and raise ABSENCE
    notifications (absence is the *lack* of a punch, so it can't be triggered on
    ingestion). Lateness is NOT notified here — it rides on the immediate
    per-punch notification (Phase 10), so we never double-notify. Idempotent.

    Holidays are no longer skipped outright: process_day decides per employee
    whether a given holiday needs attendance at all (the before/after bridging
    rule - see ``holiday_requires_attendance``). Because the "after" side of
    that rule can only be known once that day has actually happened, recent
    holidays are re-settled here too, once the day in question has passed.
    """
    from apps.notifications.tasks import raise_attendance_notification

    target = _coerce_date(date)
    employees = list(Employee.objects.filter(is_active=True).select_related('schedule_override'))

    processed = notified = lates = absences = 0
    for emp in employees:
        sched = get_effective_schedule(emp)
        l, a, n = _process_and_notify(emp, sched, target, raise_attendance_notification)
        if target.weekday() in sched.workdays:
            processed += 1
        lates += l
        absences += a
        notified += n

    # Re-settle any holiday in the lookback window now that another day has
    # passed and its "day after" side may finally be knowable.
    holiday_absences = holiday_notified = 0
    recent_holidays = Holiday.objects.filter(
        date__lt=target, date__gte=target - timedelta(days=HOLIDAY_REEVAL_DAYS))
    for holiday in recent_holidays:
        for emp in employees:
            sched = get_effective_schedule(emp)
            _, a, n = _process_and_notify(emp, sched, holiday.date, raise_attendance_notification)
            holiday_absences += a
            holiday_notified += n

    summary = {'date': target.isoformat(), 'employees': processed,
               'lates': lates, 'absences': absences, 'notifications': notified,
               'holiday_absences': holiday_absences, 'holiday_notifications': holiday_notified}
    logger.info('process_daily_attendance_task: %s', summary)
    return summary
