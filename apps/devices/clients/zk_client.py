"""Real ZKTeco backend built on the ``pyzk`` library.

This talks to a physical fingerprint unit over LAN. It cannot be exercised in
Phase 1 (no hardware), but it implements the pyzk API correctly so switching
``BIOMETRIC_DEVICE_BACKEND=zk`` later is the only change required.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from .base import BaseDeviceClient, RawPunch, RawUser

logger = logging.getLogger('apps.devices')


def _aware(ts: datetime) -> datetime:
    """pyzk returns naive datetimes in the unit's local clock; interpret them in
    TIME_ZONE so they compare with aware datetimes and store correctly."""
    return timezone.make_aware(ts) if timezone.is_naive(ts) else ts


class ZKDeviceClient(BaseDeviceClient):
    def __init__(self, ip: str | None = None, port: int | None = None,
                 timeout: int | None = None):
        self.ip = ip or settings.ZK_DEVICE_IP
        self.port = port or settings.ZK_DEVICE_PORT
        self.timeout = timeout or settings.ZK_DEVICE_TIMEOUT

    @contextmanager
    def _connection(self):
        """Connect, yield the live connection, always disconnect cleanly."""
        # Imported lazily so the dependency is only required for the real backend.
        from zk import ZK

        # ommit_ping: pyzk pre-checks with the `ping` binary, which slim Docker
        # images lack; the TCP connect itself reports an unreachable unit.
        zk = ZK(self.ip, port=self.port, timeout=self.timeout, ommit_ping=True)
        conn = None
        try:
            conn = zk.connect()
            # Freeze the device while we read so records don't shift under us.
            conn.disable_device()
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.enable_device()
                finally:
                    conn.disconnect()

    def fetch_attendance(self, since: datetime | None = None) -> list[RawPunch]:
        try:
            with self._connection() as conn:
                records = conn.get_attendance() or []
        except Exception:  # pragma: no cover - hardware path
            logger.exception('ZK fetch_attendance failed for %s:%s', self.ip, self.port)
            return []

        punches: list[RawPunch] = []
        for rec in records:
            timestamp = _aware(rec.timestamp)
            if since is not None and timestamp < since:
                continue
            punches.append(
                RawPunch(
                    biometric_id=str(rec.user_id),
                    timestamp=timestamp,
                    status=getattr(rec, 'punch', getattr(rec, 'status', None)),
                )
            )
        return punches

    def fetch_users(self) -> list[RawUser]:
        try:
            with self._connection() as conn:
                users = conn.get_users() or []
        except Exception:  # pragma: no cover - hardware path
            logger.exception('ZK fetch_users failed for %s:%s', self.ip, self.port)
            return []
        return [RawUser(biometric_id=str(u.user_id), name=u.name or '') for u in users]

    def test_connection(self) -> bool:
        try:
            with self._connection():
                return True
        except Exception:  # pragma: no cover - hardware path
            logger.warning('ZK device unreachable at %s:%s', self.ip, self.port)
            return False

    def live_capture(self):  # pragma: no cover - hardware path
        """Stream punches in real time via pyzk's live_capture(). Long-running:
        yields a RawPunch the instant the unit registers a fingerprint. This is
        what makes biometric punches truly instant (vs. interval polling)."""
        from zk import ZK

        # ommit_ping: pyzk pre-checks with the `ping` binary, which slim Docker
        # images lack; the TCP connect itself reports an unreachable unit.
        zk = ZK(self.ip, port=self.port, timeout=self.timeout, ommit_ping=True)
        conn = zk.connect()
        try:
            for attendance in conn.live_capture():
                if attendance is None:
                    continue  # periodic keep-alive tick from pyzk
                yield RawPunch(
                    biometric_id=str(attendance.user_id),
                    timestamp=_aware(attendance.timestamp),
                    status=getattr(attendance, 'punch',
                                   getattr(attendance, 'status', None)),
                )
        finally:
            try:
                conn.end_live_capture = True
            finally:
                conn.disconnect()
