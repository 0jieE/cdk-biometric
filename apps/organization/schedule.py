"""Effective-schedule resolver.

All attendance processing and the API must go through ``get_effective_schedule``
— never read GlobalSchedule / EmployeeSchedule rows directly — so per-employee
overrides and global fallback are applied consistently in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class EffectiveSchedule:
    am_in: time
    am_out: time
    pm_in: time
    pm_out: time
    grace_period_minutes: int
    workdays: list

    def session_in(self, session: str) -> time:
        return self.am_in if session == 'AM' else self.pm_in

    @property
    def midpoint(self) -> time:
        """Boundary between AM and PM punches: midpoint of am_out↔pm_in.
        Falls back to 12:30 if the values are unusable."""
        try:
            a = self.am_out.hour * 60 + self.am_out.minute
            b = self.pm_in.hour * 60 + self.pm_in.minute
            m = (a + b) // 2
            return time(m // 60, m % 60)
        except Exception:  # pragma: no cover - defensive
            return time(12, 30)


def get_effective_schedule(employee) -> EffectiveSchedule:
    """Resolve an employee's schedule: per-employee override where set, else the
    institution global default. A null override field inherits the global."""
    from .models import GlobalSchedule

    g = GlobalSchedule.load()
    override = getattr(employee, 'schedule_override', None)

    def pick(field):
        value = getattr(override, field, None) if override is not None else None
        return getattr(g, field) if value is None else value

    return EffectiveSchedule(
        am_in=pick('am_in'),
        am_out=pick('am_out'),
        pm_in=pick('pm_in'),
        pm_out=pick('pm_out'),
        grace_period_minutes=pick('grace_period_minutes'),
        workdays=pick('workdays'),
    )
