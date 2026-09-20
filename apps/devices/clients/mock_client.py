"""Deterministic biometric simulator (Phase 8).

Exercises every processing path against the new schedule/OT model:

  * Full-time: up to 4 punches near effective am/pm in/out with jitter; some late
    (AM and/or PM past grace); occasionally a whole session dropped (=> half-day)
    or a whole day dropped (=> absent).
  * Authorized overtime: some full-time employees, on some days, get an
    OTAuthorization + OT_IN/OT_OUT punches after ot_start (=> real Overtime).
  * Unauthorized late-leaver: a different full-time employee punches past pm_out
    with NO authorization (=> must yield NO overtime — proves the rule).
  * Part-time: in/out only; occasionally one dropped (=> incomplete absent) or
    both dropped (=> plain absent).

Output is deterministic per (employee, day) so repeat syncs dedupe and tests are
stable.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.utils import timezone

from .base import BaseDeviceClient, RawPunch, RawUser

STATUS_IN = 0
STATUS_OUT = 1
DEFAULT_LOOKBACK_DAYS = 7


def _seeded_rng(biometric_id: str, day: date_cls, salt: str = '') -> random.Random:
    digest = hashlib.md5(f'{biometric_id}:{day.isoformat()}:{salt}'.encode()).hexdigest()
    return random.Random(int(digest, 16))


def _bio_int(biometric_id: str) -> int:
    try:
        return int(biometric_id)
    except (TypeError, ValueError):
        return sum(ord(c) for c in biometric_id)


class MockDeviceClient(BaseDeviceClient):
    def fetch_attendance(self, since: datetime | None = None) -> list[RawPunch]:
        from apps.attendance.models import OTAuthorization
        from apps.organization.models import Employee, Holiday
        from apps.organization.schedule import get_effective_schedule

        now = timezone.now()
        if since is None:
            since = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        start_day = timezone.localtime(since).date()
        end_day = timezone.localtime(now).date()

        holidays = set(Holiday.objects.values_list('date', flat=True))
        tz = timezone.get_current_timezone()

        employees = (
            Employee.objects.filter(is_active=True)
            .exclude(biometric_id='')
            .select_related('schedule_override')
        )

        punches: list[RawPunch] = []
        for emp in employees:
            sched = get_effective_schedule(emp)
            bio = emp.biometric_id
            n = _bio_int(bio)
            chronic_late = n % 4 == 0
            ot_employee = emp.is_fulltime and n % 5 == 0
            late_leaver = emp.is_fulltime and n % 5 == 1

            day = start_day
            while day <= end_day:
                if day.weekday() in sched.workdays and day not in holidays:
                    if emp.is_fulltime:
                        punches.extend(self._fulltime_day(
                            bio, day, sched, chronic_late, ot_employee,
                            late_leaver, emp, tz, OTAuthorization))
                    else:
                        punches.extend(self._parttime_day(bio, day, sched, tz))
                day += timedelta(days=1)

        return [p for p in punches if p.timestamp >= since]

    # -- full-time ---------------------------------------------------------
    def _fulltime_day(self, bio, day, sched, chronic_late, ot_employee,
                      late_leaver, emp, tz, OTAuthorization) -> list[RawPunch]:
        rng = _seeded_rng(bio, day)

        # Whole-day absence.
        if rng.random() < 0.05:
            return []

        out: list[RawPunch] = []
        grace = sched.grace_period_minutes

        drop_am = rng.random() < 0.08
        drop_pm = rng.random() < 0.08 and not drop_am  # avoid dropping both here

        if not drop_am:
            am_in_off = (grace + rng.randint(3, 15)) if chronic_late else rng.randint(-4, min(2, grace))
            out.append(self._punch(bio, day, sched.am_in, am_in_off, STATUS_IN, tz))
            out.append(self._punch(bio, day, sched.am_out, rng.randint(-10, 5), STATUS_OUT, tz))

        if not drop_pm:
            pm_in_off = (grace + rng.randint(3, 15)) if chronic_late else rng.randint(-4, min(2, grace))
            out.append(self._punch(bio, day, sched.pm_in, pm_in_off, STATUS_IN, tz))
            out.append(self._punch(bio, day, sched.pm_out, rng.randint(-5, 15), STATUS_OUT, tz))

        # Authorized overtime on ~half of this employee's days.
        if ot_employee and not drop_pm and _seeded_rng(bio, day, 'ot').random() < 0.5:
            ot_start = _add_minutes(sched.pm_out, 30)  # OT begins 30m after pm_out
            OTAuthorization.objects.get_or_create(
                employee=emp, date=day,
                defaults={'ot_start': ot_start, 'note': 'auto (mock simulator)'},
            )
            out.append(self._punch(bio, day, ot_start, rng.randint(2, 8), STATUS_IN, tz))
            out.append(self._punch(bio, day, ot_start, rng.randint(90, 150), STATUS_OUT, tz))

        # Unauthorized late-leaver: extra punch well past pm_out, NO authorization.
        elif late_leaver and _seeded_rng(bio, day, 'late').random() < 0.4:
            out.append(self._punch(bio, day, sched.pm_out, rng.randint(45, 90), STATUS_OUT, tz))

        return out

    # -- part-time ---------------------------------------------------------
    def _parttime_day(self, bio, day, sched, tz) -> list[RawPunch]:
        rng = _seeded_rng(bio, day)

        if rng.random() < 0.06:
            return []  # no punch -> plain absent

        in_punch = self._punch(bio, day, sched.am_in, rng.randint(-10, 20), STATUS_IN, tz)
        out_punch = self._punch(bio, day, sched.pm_out, rng.randint(-15, 20), STATUS_OUT, tz)

        drop = rng.random()
        if drop < 0.12:
            # Incomplete: keep exactly one punch.
            return [in_punch] if rng.random() < 0.5 else [out_punch]
        return [in_punch, out_punch]

    # -- helpers -----------------------------------------------------------
    def _punch(self, bio, day, base_time: time, minute_offset: int, status, tz) -> RawPunch:
        naive = datetime.combine(day, base_time) + timedelta(minutes=minute_offset)
        return RawPunch(biometric_id=bio, timestamp=timezone.make_aware(naive, tz), status=status)

    def fetch_users(self) -> list[RawUser]:
        from apps.organization.models import Employee
        return [
            RawUser(biometric_id=e.biometric_id, name=e.full_name)
            for e in Employee.objects.filter(is_active=True).exclude(biometric_id='')
        ]

    def test_connection(self) -> bool:
        return True


def _add_minutes(t: time, minutes: int) -> time:
    total = (t.hour * 60 + t.minute + minutes) % (24 * 60)
    return time(total // 60, total % 60)
