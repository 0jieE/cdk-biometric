"""Per-punch notification composition + the ingestion hook.

Called the moment an AttendanceLog is persisted (device sync, live capture, or
manual entry). Idempotent via the OneToOne ``Notification.attendance_log`` link,
and it enqueues the immediate send task straight away (not the daily batch).
"""

from __future__ import annotations

import logging

from django.utils import timezone

from .models import Notification

logger = logging.getLogger('apps.notifications')

# Punches that are a session "time in" (and can therefore be late, full-time).
SESSION_IN_TYPES = {'AM_IN', 'PM_IN'}


def _format_12h(dt) -> str:
    local = timezone.localtime(dt)
    hour = local.hour % 12 or 12
    ampm = 'AM' if local.hour < 12 else 'PM'
    return f'{hour}:{local.minute:02d} {ampm}'


def _late_minutes_for(log) -> int:
    """Exact tardiness for a full-time session IN punch (0 otherwise)."""
    if log.log_type not in SESSION_IN_TYPES or not log.employee.is_fulltime:
        return 0
    from apps.attendance.services import minutes_late
    from apps.organization.schedule import get_effective_schedule

    sched = get_effective_schedule(log.employee)
    session_in = sched.am_in if log.log_type == 'AM_IN' else sched.pm_in
    local_time = timezone.localtime(log.log_datetime).time()
    return minutes_late(local_time, session_in, sched.grace_period_minutes)


def compose_punch_message(log) -> tuple[str, str]:
    """Return (title, body) for a punch, e.g. 'AM In recorded — 8:02 AM' or
    'AM In — 8:07 AM (Late by 7 min)'."""
    label = log.get_log_type_display()          # e.g. "AM In"
    time_str = _format_12h(log.log_datetime)
    late = _late_minutes_for(log)
    if late > 0:
        body = f'{label} — {time_str} (Late by {late} min)'
    else:
        body = f'{label} recorded — {time_str}'
    return 'Attendance Recorded', body


def notify_for_log(log) -> tuple[Notification | None, bool]:
    """Create (idempotently) one notification for this log and enqueue an
    immediate send. Returns (notification, created)."""
    title, body = compose_punch_message(log)
    notification, created = Notification.objects.get_or_create(
        attendance_log=log,
        defaults={
            'employee': log.employee,
            'type': Notification.Types.PUNCH,
            'title': title,
            'body': body,
        },
    )
    if created:
        _enqueue_on_commit(notification.id)
        _publish_live_on_commit(log)
    return notification, created


def _enqueue_on_commit(notification_id):
    """Enqueue the immediate send after the surrounding DB transaction commits,
    so the worker never races ahead of the committed Notification/log rows.
    (Runs immediately if there is no open transaction.)"""
    from django.db import transaction
    from .tasks import send_fcm_notification_task

    def _enqueue():
        try:
            send_fcm_notification_task.delay(notification_id)
        except Exception:
            # Broker/worker down (e.g. brownout) — the sweeper delivers it once
            # things recover. Never fail ingestion because of this.
            logger.warning('Immediate send enqueue failed for notification %s; '
                           'sweeper will retry.', notification_id)

    transaction.on_commit(_enqueue)


def _publish_live_on_commit(log):
    """Wake the /live/ kiosk page immediately once the log is actually
    committed (so its refresh is guaranteed to see the new row)."""
    from django.db import transaction

    from apps.attendance.realtime import publish_new_log

    transaction.on_commit(lambda: publish_new_log(log))
