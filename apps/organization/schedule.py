"""Effective-schedule resolver.

All attendance processing and the API must go through ``get_effective_schedule``
— never read GlobalSchedule / EmployeeSchedule rows directly — so per-employee
overrides and global fallback are applied consistently in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal


@dataclass(frozen=True)
class EffectiveSchedule:
    am_in: time
    am_out: time
    pm_in: time
    pm_out: time
    grace_period_minutes: int
    workdays: list
    midpoint: time = time(12, 30)  # AM/PM boundary for classifying punches
    weekly_hours: Decimal = Decimal('20')  # part-time weekly requirement
    weekly_min_days: int = 2               # ...over at least this many days

    @property
    def weekly_minutes(self) -> int:
        return int(self.weekly_hours * 60)

    def session_in(self, session: str) -> time:
        return self.am_in if session == 'AM' else self.pm_in

    def session_minutes(self, session: str) -> int:
        """Scheduled working minutes of a session: 'AM', 'PM', or anything
        else (a whole day, e.g. a part-timer's FULL absence) for both."""
        def span(start, end):
            return max(0, (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute))

        am = span(self.am_in, self.am_out)
        pm = span(self.pm_in, self.pm_out)
        if session == 'AM':
            return am
        if session == 'PM':
            return pm
        return am + pm


def get_effective_schedule(employee) -> EffectiveSchedule:
    """Resolve an employee's schedule: per-employee override where set, else the
    institution global default. A null override field inherits the global."""
    from .models import GlobalSchedule

    g = GlobalSchedule.load()
    override = getattr(employee, 'schedule_override', None)

    def pick(field, global_field=None):
        value = getattr(override, field, None) if override is not None else None
        return getattr(g, global_field or field) if value is None else value

    return EffectiveSchedule(
        am_in=pick('am_in'),
        am_out=pick('am_out'),
        pm_in=pick('pm_in'),
        pm_out=pick('pm_out'),
        grace_period_minutes=pick('grace_period_minutes'),
        workdays=pick('workdays'),
        midpoint=pick('midpoint'),
        weekly_hours=Decimal(pick('weekly_hours', 'parttime_weekly_hours')),
        weekly_min_days=g.parttime_min_days,
    )
