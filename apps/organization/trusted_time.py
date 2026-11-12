"""Accurate 'now' for attendance decisions, independent of this server's own
clock. The server's clock can drift (a wrong BIOS/VM clock, a missed NTP sync,
a manual change) - rather than trust it outright, periodically check it
against an online time source and keep using the last known-good gap, even
while offline.

``refresh_offset()`` does the check; it's called from the periodic attendance
sync (every ``SYNC_INTERVAL_MINUTES``), so no extra scheduling is needed.

Each good check also saves an *anchor*: the real time, plus the machine's
uptime clock (CLOCK_BOOTTIME) at that moment, plus the kernel boot id. Until
the machine reboots, ``trusted_now()`` is anchor + elapsed uptime - the uptime
clock only counts forward and is not touched when someone changes the PC's
date/time, so such a change has no effect, online or offline. (All Docker
containers share one kernel, so web and the workers share the same anchor.)
After a reboot the anchor no longer applies, and the older correction
(wall clock + last measured offset) is used until the next good check.
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


def _boot_id() -> str:
    """This kernel boot's unique id; '' where unavailable (e.g. Windows dev)."""
    try:
        with open('/proc/sys/kernel/random/boot_id') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def _uptime() -> float:
    """Seconds since boot on a clock that wall-clock changes can't move.
    CLOCK_BOOTTIME also counts time the machine spent suspended."""
    import time

    clock = getattr(time, 'CLOCK_BOOTTIME', None)
    if clock is not None:
        try:
            return time.clock_gettime(clock)
        except OSError:
            pass
    return time.monotonic()


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
    sync.anchor_utc, sync.anchor_uptime, sync.anchor_boot_id = online, _uptime(), _boot_id()
    sync.save(update_fields=['offset_seconds', 'last_synced_at', 'last_sync_ok',
                             'anchor_utc', 'anchor_uptime', 'anchor_boot_id'])
    return True


def trusted_now() -> datetime:
    """Real time, as best known:

    1. Since the last good online check, on this same boot: that check's time
       plus the uptime elapsed since - immune to wall-clock changes.
    2. Otherwise (rebooted since, or never checked): this server's clock
       corrected by the last measured gap; plain system clock if never synced.
    """
    from .models import TimeSync

    sync = TimeSync.objects.filter(pk=1).first()
    if sync is None:
        return timezone.now()
    if sync.anchor_utc and sync.anchor_uptime is not None and sync.anchor_boot_id:
        if sync.anchor_boot_id == _boot_id():
            elapsed = _uptime() - sync.anchor_uptime
            if elapsed >= 0:
                return sync.anchor_utc + timedelta(seconds=elapsed)
    return timezone.now() + timedelta(seconds=sync.offset_seconds)


def trusted_localdate() -> date_cls:
    return timezone.localtime(trusted_now()).date()
