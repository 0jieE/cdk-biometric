"""Firebase Cloud Messaging integration.

If ``FIREBASE_CREDENTIALS`` is blank (the Phase 2 default), the whole module
no-ops gracefully — the system runs fine without Firebase configured. Once a
service-account JSON path is provided, ``send_to_employee`` delivers to every
active MobileDevice token and prunes tokens the server reports as unregistered.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('apps.notifications')

_initialized = False
_enabled = False


def _ensure_initialized() -> bool:
    """Lazily initialise firebase-admin. Returns True if FCM is usable."""
    global _initialized, _enabled
    if _initialized:
        return _enabled

    _initialized = True
    cred_path = getattr(settings, 'FIREBASE_CREDENTIALS', '')
    if not cred_path:
        logger.info('FCM disabled (FIREBASE_CREDENTIALS is blank).')
        _enabled = False
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(cred_path))
        _enabled = True
        project_id = getattr(firebase_admin.get_app(), 'project_id', None)
        logger.info('FCM enabled (project=%s, credentials=%s)', project_id, cred_path)
    except Exception:
        logger.exception('FCM initialisation failed — sending disabled.')
        _enabled = False

    return _enabled


def send_to_employee(notification) -> dict:
    """Send one Notification to the employee's active device tokens.

    Returns a summary dict:
      ``enabled``   — False when FCM is not configured (no-op, don't retry).
      ``sent``      — number of tokens delivered to.
      ``failed``    — number of dead tokens pruned (permanent, don't retry).
      ``transient`` — True if a retryable error occurred (network / FCM
                      unavailable / timeout); the caller should retry.
      ``error``     — last transient error string, if any.

    Permanent errors (``UnregisteredError`` / ``SenderIdMismatchError`` /
    malformed token) prune the token and are NOT retried. Everything else is
    treated as transient so the send task retries.
    """
    if not _ensure_initialized():
        return {'sent': 0, 'failed': 0, 'enabled': False, 'transient': False, 'error': None}

    from firebase_admin import messaging
    from apps.devices.models import MobileDevice

    tokens = list(
        MobileDevice.objects
        .filter(employee=notification.employee, is_active=True)
        .exclude(fcm_token='')
        .values_list('fcm_token', flat=True)
    )
    if not tokens:
        logger.info('No active tokens for employee %s', notification.employee_id)
        return {'sent': 0, 'failed': 0, 'enabled': True, 'transient': False, 'error': None}

    sent = 0
    dead_tokens = []
    transient = False
    last_error = None
    for token in tokens:
        message = messaging.Message(
            notification=messaging.Notification(
                title=notification.title, body=notification.body),
            token=token,
        )
        try:
            messaging.send(message)
            sent += 1
        except (messaging.UnregisteredError, messaging.SenderIdMismatchError, ValueError):
            # Permanent: dead / wrong-sender / malformed token — prune, no retry.
            dead_tokens.append(token)
        except Exception as exc:
            # Network / FCM-unavailable / timeout / unknown -> retryable.
            transient = True
            last_error = str(exc)
            logger.warning('Transient FCM error for token %s: %s', token[:12], exc)

    if dead_tokens:
        MobileDevice.objects.filter(fcm_token__in=dead_tokens).update(is_active=False)
        logger.info('Pruned %d dead FCM token(s)', len(dead_tokens))

    return {'sent': sent, 'failed': len(dead_tokens), 'enabled': True,
            'transient': transient, 'error': last_error}
