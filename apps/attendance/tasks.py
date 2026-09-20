"""Celery tasks for attendance ingestion and daily processing."""

from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta

from celery import shared_task
from django.utils import timezone

from apps.organization.models import Employee, Holiday
from apps.organization.schedule import get_effective_schedule

from .models import Absence
from .services import process_day, sync_attendance

logger = logging.getLogger('apps.attendance')


@shared_task(name='attendance.sync_attendance')
def sync_attendance_task():
    """Pull punches from the configured backend (called by Celery Beat), then
    process today's derived rows + notifications."""
    from apps.devices.models import BiometricDevice

    device = BiometricDevice.objects.filter(is_active=True).first()
    since = timezone.now() - timedelta(days=1)
    summary = sync_attendance(device=device, since=since)

    process_daily_attendance_task(timezone.localdate().isoformat())
    logger.info('sync_attendance_task done: %s', summary)
    return summary


def _coerce_date(value) -> date_cls:
    if isinstance(value, date_cls):
        return value
    return datetime.strptime(value, '%Y-%m-%d').date()


@shared_task(name='attendance.process_daily_attendance')
def process_daily_attendance_task(date):
    """Recompute one date for every active employee and raise ABSENCE
    notifications (absence is the *lack* of a punch, so it can't be triggered on
    ingestion). Lateness is NOT notified here — it rides on the immediate
    per-punch notification (Phase 10), so we never double-notify. Idempotent.
    """
    from apps.notifications.tasks import raise_attendance_notification

    target = _coerce_date(date)
    if Holiday.objects.filter(date=target).exists():
        return {'date': target.isoformat(), 'skipped': 'holiday'}

    processed = notified = lates = absences = 0
    for emp in Employee.objects.filter(is_active=True).select_related('schedule_override'):
        sched = get_effective_schedule(emp)
        if target.weekday() not in sched.workdays:
            continue

        result = process_day(emp, target)
        processed += 1
        lates += result['lates']

        for ab in Absence.objects.filter(employee=emp, date=target):
            absences += 1
            label = 'ABSENT' if not ab.incomplete else f'ABSENT ({ab.get_reason_display()})'
            scope = '' if ab.session == 'FULL' else f' [{ab.session}]'
            if raise_attendance_notification(
                emp, 'ABSENCE', title='Marked Absent',
                body=f'You were marked {label}{scope} on {target:%Y-%m-%d}.'):
                notified += 1

    summary = {'date': target.isoformat(), 'employees': processed,
               'lates': lates, 'absences': absences, 'notifications': notified}
    logger.info('process_daily_attendance_task: %s', summary)
    return summary
