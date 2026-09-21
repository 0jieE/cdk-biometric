"""When did real attendance tracking begin?

Days before this date carry no attendance information — nobody was being
recorded — so they must never be reported as present *or* absent, and must
never be backfilled with absences. Used by the API / portal / report selectors
and by the ``backfill_attendance`` command.

A day that has real punches is always shown, whatever this says; the start date
only limits days that would otherwise be *inferred* (a workday with no data).
"""

from __future__ import annotations

from datetime import date as date_cls

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Min
from django.utils import timezone

from .models import AttendanceLog


def configured_start() -> date_cls | None:
    """``ATTENDANCE_START_DATE`` (YYYY-MM-DD) if set, else None."""
    raw = (getattr(settings, 'ATTENDANCE_START_DATE', '') or '').strip()
    if not raw:
        return None
    try:
        return date_cls.fromisoformat(raw)
    except ValueError:
        raise ImproperlyConfigured(
            f'ATTENDANCE_START_DATE must be YYYY-MM-DD, got {raw!r}')


def system_start() -> date_cls:
    """The configured start date, else the day of the earliest recorded punch,
    else today (nothing has been recorded yet, so nothing is inferable)."""
    configured = configured_start()
    if configured is not None:
        return configured
    first = AttendanceLog.objects.aggregate(first=Min('log_datetime'))['first']
    return timezone.localtime(first).date() if first else timezone.localdate()


def employee_floor(employee) -> date_cls:
    """Earliest day an employee can be inferred absent: the day they were added
    to the system, or their hire date if that is later."""
    floor = timezone.localtime(employee.created_at).date()
    if employee.date_hired:
        floor = max(floor, employee.date_hired)
    return floor


def tracking_start(employee) -> date_cls:
    """First day this employee's absences may be inferred."""
    return max(system_start(), employee_floor(employee))
