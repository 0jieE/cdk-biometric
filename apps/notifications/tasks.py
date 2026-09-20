"""Celery tasks for notification delivery.

Delivery model:
  * immediate  — ``send_fcm_notification_task`` sends once; on a *transient*
    failure Celery retries with exponential backoff (failure-only retry).
  * permanent  — dead/invalid tokens are pruned, never retried.
  * outage     — ``retry_pending_notifications_task`` (Celery Beat, ~2 min) re-
    enqueues anything still PENDING after a worker/power outage, so nothing is
    lost when the worker was down at ingestion time.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Notification

logger = logging.getLogger('apps.notifications')

# Sweeper tuning.
SWEEP_MIN_AGE_SECONDS = 15    # let the immediate send try first
SWEEP_MAX_ATTEMPTS = 50       # give up (FAILED) after this many total attempts
SWEEP_BATCH = 200


class TransientFCMError(Exception):
    """Retryable delivery failure (network / FCM unavailable / timeout)."""


def raise_attendance_notification(employee, ntype, title, body) -> bool:
    """Idempotently create a non-punch (absence) notification + enqueue send.
    Idempotency key is (employee, type, body); body carries the date."""
    notification, created = Notification.objects.get_or_create(
        employee=employee, type=ntype, body=body,
        defaults={'title': title},
    )
    if created:
        try:
            send_fcm_notification_task.delay(notification.id)
        except Exception:
            logger.warning('Enqueue failed for notification %s; sweeper will retry.',
                           notification.id)
    return created


def _deliver(notification) -> str:
    """Attempt one delivery, updating tracking fields. Returns the outcome:
    'sent' | 'disabled' | 'retry'."""
    from .fcm import send_to_employee

    notification.attempts += 1
    result = send_to_employee(notification)

    if not result['enabled']:
        # FCM not configured — nothing to deliver to; terminal (don't sweep).
        notification.status = Notification.Status.SENT
        notification.last_error = 'FCM disabled'
        notification.save(update_fields=['attempts', 'status', 'last_error'])
        return 'disabled'

    if result['transient'] and result['sent'] == 0:
        notification.status = Notification.Status.PENDING
        notification.last_error = result.get('error') or 'transient error'
        notification.save(update_fields=['attempts', 'status', 'last_error'])
        return 'retry'

    # Delivered (or no active tokens to deliver to) — terminal success.
    notification.status = Notification.Status.SENT
    notification.sent_at = timezone.now()
    notification.last_error = ''
    notification.save(update_fields=['attempts', 'status', 'sent_at', 'last_error'])
    return 'sent'


@shared_task(
    name='notifications.send_fcm',
    bind=True,
    autoretry_for=(TransientFCMError,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=24,
)
def send_fcm_notification_task(self, notification_id):
    """Deliver one notification now; retry only on transient failure."""
    try:
        notification = Notification.objects.select_related('employee').get(pk=notification_id)
    except Notification.DoesNotExist:
        logger.warning('Notification %s no longer exists', notification_id)
        return {'status': 'MISSING'}

    outcome = _deliver(notification)
    if outcome == 'retry':
        # Raise so Celery retries with backoff; stays PENDING for the sweeper too.
        raise TransientFCMError(notification.last_error or 'transient FCM error')
    return {'status': notification.status, 'outcome': outcome, 'attempts': notification.attempts}


@shared_task(name='notifications.retry_pending')
def retry_pending_notifications_task():
    """Outage recovery: re-enqueue notifications stuck PENDING (e.g. captured
    while the worker/host was down). Runs on a short Beat schedule."""
    cutoff = timezone.now() - timedelta(seconds=SWEEP_MIN_AGE_SECONDS)
    pending = (
        Notification.objects
        .filter(status=Notification.Status.PENDING, sent_at__isnull=True, created_at__lt=cutoff)
        .order_by('created_at')[:SWEEP_BATCH]
    )

    reenqueued = failed = 0
    for notification in pending:
        if notification.attempts >= SWEEP_MAX_ATTEMPTS:
            notification.status = Notification.Status.FAILED
            notification.save(update_fields=['status'])
            failed += 1
            continue
        try:
            send_fcm_notification_task.delay(notification.id)
            reenqueued += 1
        except Exception:
            logger.warning('Sweeper could not enqueue notification %s', notification.id)

    if reenqueued or failed:
        logger.info('sweeper: reenqueued=%s failed=%s', reenqueued, failed)
    return {'reenqueued': reenqueued, 'failed': failed}
