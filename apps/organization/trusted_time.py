"""Accurate 'now' for attendance decisions, independent of this server's own
clock. The server's clock can drift (a wrong BIOS/VM clock, a missed NTP sync,
a manual change) - rather than trust it outright, periodically check it
against an online time source and keep using the last known-good gap, even
while offline.

``refresh_offset()`` does the check; it's called from the periodic attendance
sync (every ``SYNC_INTERVAL_MINUTES``), so no extra scheduling is needed.
``trusted_now()`` / ``trusted_localdate()`` are the drop-in replacements for
``timezone.now()`` / ``timezone.localdate()`` wherever "what time/day is it"
actually drives an attendance decision or what the portal displays as today.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('apps.organization')


def _fetch_online_time() -> datetime | None:
    """Real UTC time from an internet time source, or None if unreachable."""
    import requests

    url = getattr(settings, 'TRUSTED_TIME_URL', 'https://www.google.com')
    timeout = getattr(settings, 'TRUSTED_TIME_TIMEOUT', 3)
    try:
        resp = requests.head(url, timeout=timeout)
        value = parsedate_to_datetime(resp.headers['Date'])
    except Exception:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.utc)
    return value


def refresh_offset() -> bool:
    """Compare this server's clock against the online source and persist the
    gap. Safe to call when offline: the previously saved offset is simply
    kept. Returns whether the check reached the online source."""
    from .models import TimeSync

    online = _fetch_online_time()
    sync = TimeSync.load()
    if online is None:
        if sync.last_sync_ok:
            logger.warning('Trusted-time check unreachable; keeping last known offset (%+.1fs from %s).',
                           sync.offset_seconds, sync.last_synced_at)
        sync.last_sync_ok = False
        sync.save(update_fields=['last_sync_ok'])
        return False

    sync.offset_seconds = (online - timezone.now()).total_seconds()
    sync.last_synced_at = timezone.now()
    sync.last_sync_ok = True
    sync.save(update_fields=['offset_seconds', 'last_synced_at', 'last_sync_ok'])
    return True


def trusted_now() -> datetime:
    """This server's clock, corrected by the last known gap against real
    time. 0 offset (plain system clock) until the first successful sync."""
    from .models import TimeSync

    sync = TimeSync.objects.filter(pk=1).first()
    offset = sync.offset_seconds if sync else 0
    return timezone.now() + timedelta(seconds=offset)


def trusted_localdate() -> date_cls:
    return timezone.localtime(trusted_now()).date()
