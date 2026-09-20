"""Push signal for the live-log kiosk page.

The AttendanceLog row is already the source of truth by the time this runs;
this only tells any open ``/live/`` connections "something changed" via Redis
pub/sub, so the browser refreshes the moment a punch lands instead of waiting
for the next poll. Reuses the existing Celery broker connection — no new
infra, no new setting.

Never allowed to break ingestion or notifications: a publish failure (Redis
down) is logged and swallowed, exactly like the FCM enqueue it sits next to.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger('apps.attendance')

LIVE_PUNCH_CHANNEL = 'live_punches'


def publish_new_log(log) -> None:
    """Announce a newly-created AttendanceLog to any subscribed live pages."""
    try:
        import redis
        client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
        try:
            client.publish(LIVE_PUNCH_CHANNEL, str(getattr(log, 'id', '')))
        finally:
            client.close()
    except Exception:
        logger.warning(
            'live-log publish failed for AttendanceLog %s; the kiosk page '
            'will still pick it up on its fallback poll.',
            getattr(log, 'id', '?'))
