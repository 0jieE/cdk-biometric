"""Abstract device-client interface and the raw data structures it returns."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawPunch:
    """A single unprocessed punch as read from a device.

    ``biometric_id`` is the enrollment/UID on the fingerprint unit (mapped back
    to an Employee by the sync service). ``status`` is the raw punch code the
    device reports (e.g. check-in/check-out), or ``None`` if unavailable.
    """

    biometric_id: str
    timestamp: datetime
    status: int | None = None


@dataclass
class RawUser:
    """A single enrolled user as read from a device."""

    biometric_id: str
    name: str = ''


class BaseDeviceClient(abc.ABC):
    """Common interface every biometric backend must implement."""

    @abc.abstractmethod
    def fetch_attendance(self, since: datetime | None = None) -> list[RawPunch]:
        """Return punches, optionally only those at/after ``since``."""

    @abc.abstractmethod
    def fetch_users(self) -> list[RawUser]:
        """Return the users enrolled on the device."""

    @abc.abstractmethod
    def test_connection(self) -> bool:
        """Return True if the device is reachable."""

    def live_capture(self):
        """Yield RawPunch objects the instant they happen (real-time path).

        Optional: backends that can't stream (e.g. the mock) raise
        NotImplementedError and callers fall back to interval polling.
        """
        raise NotImplementedError(
            f'{type(self).__name__} does not support live_capture; use polling.')
