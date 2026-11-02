"""Phase 8 attendance logic tests."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.attendance.models import (
    Absence,
    AttendanceLog,
    Lates,
    OTAuthorization,
    Overtime,
    Undertime,
)
from apps.attendance.services import (
    assign_fulltime_punches,
    assign_parttime_punches,
    ingest_punches,
    minutes_late,
    minutes_undertime,
    process_day,
)
from apps.devices.clients import RawPunch
from apps.notifications.models import Notification
from apps.organization.models import Department, Employee, GlobalSchedule, Holiday

WORKDAY = date(2026, 7, 6)   # Monday


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class PunchAssignmentTests(TestCase):
    """Pure classification of punches into session log types."""

    def test_no_authorization_no_ot_window(self):
        times = [aware(WORKDAY, time(8, 0)), aware(WORKDAY, time(12, 0)),
                 aware(WORKDAY, time(13, 0)), aware(WORKDAY, time(17, 0)),
                 aware(WORKDAY, time(18, 30))]  # late leave, no auth
        result = assign_fulltime_punches(times, time(12, 30), ot_start=None)
        self.assertNotIn('OT_IN', result)
        self.assertNotIn('OT_OUT', result)
        # The late punch just extends PM_OUT.
        self.assertEqual(result['PM_OUT'], aware(WORKDAY, time(18, 30)))

    def test_authorization_splits_ot(self):
        times = [aware(WORKDAY, time(8, 0)), aware(WORKDAY, time(12, 0)),
                 aware(WORKDAY, time(13, 0)), aware(WORKDAY, time(17, 0)),
                 aware(WORKDAY, time(17, 40)), aware(WORKDAY, time(19, 30))]
        result = assign_fulltime_punches(times, time(12, 30), ot_start=time(17, 30))
        self.assertEqual(result['PM_OUT'], aware(WORKDAY, time(17, 0)))
        self.assertEqual(result['OT_IN'], aware(WORKDAY, time(17, 40)))
        self.assertEqual(result['OT_OUT'], aware(WORKDAY, time(19, 30)))

    def test_lone_after_hours_punch_is_out_not_in(self):
        # Only punch of the day is well after PM_OUT (17:00) — must file as
        # PM_OUT with no PM_IN, not as an IN with a missing OUT.
        times = [aware(WORKDAY, time(18, 0))]
        result = assign_fulltime_punches(times, time(12, 30), pm_out=time(17, 0))
        self.assertEqual(result, {'PM_OUT': aware(WORKDAY, time(18, 0))})

    def test_two_after_hours_punches_still_have_no_in(self):
        # Two punches, both at/after PM_OUT — must NOT read as "present, just
        # late": there's no IN at all, only the latest as OUT.
        times = [aware(WORKDAY, time(17, 0)), aware(WORKDAY, time(18, 0))]
        result = assign_fulltime_punches(times, time(12, 30), pm_out=time(17, 0))
        self.assertEqual(result, {'PM_OUT': aware(WORKDAY, time(18, 0))})

    def test_normal_late_checkout_is_unaffected(self):
        # A legitimate IN before PM_OUT, followed by a late-but-real checkout
        # after PM_OUT, must still pair normally.
        times = [aware(WORKDAY, time(13, 3)), aware(WORKDAY, time(17, 10))]
        result = assign_fulltime_punches(times, time(12, 30), pm_out=time(17, 0))
        self.assertEqual(result['PM_IN'], aware(WORKDAY, time(13, 3)))
        self.assertEqual(result['PM_OUT'], aware(WORKDAY, time(17, 10)))

    def test_without_out_times_falls_back_to_earliest_latest(self):
        # am_out/pm_out are optional — omitting them keeps the old behaviour.
        times = [aware(WORKDAY, time(18, 0))]
        result = assign_fulltime_punches(times, time(12, 30))
        self.assertEqual(result, {'PM_IN': aware(WORKDAY, time(18, 0))})

    def test_parttime_pairing(self):
        r = assign_parttime_punches([aware(WORKDAY, time(17, 0)), aware(WORKDAY, time(8, 0))])
        self.assertEqual(r['IN'], aware(WORKDAY, time(8, 0)))
        self.assertEqual(r['OUT'], aware(WORKDAY, time(17, 0)))
        single = assign_parttime_punches([aware(WORKDAY, time(8, 0))])
        self.assertEqual(single, {'IN': aware(WORKDAY, time(8, 0))})


class DuplicatePunchTests(TestCase):
    """Pressing twice within 5 minutes is one punch, not an IN and an OUT."""

    def _at(self, h, m, s=0):
        return aware(WORKDAY, time(h, m, s))

    def test_a_double_tap_seconds_apart_is_just_the_in(self):
        r = assign_fulltime_punches([self._at(7, 55), self._at(7, 55, 10)], time(12, 30),
                                    am_out=time(12, 0), pm_out=time(17, 0))
        self.assertEqual(r, {'AM_IN': self._at(7, 55)})

    def test_four_minutes_59_is_still_a_duplicate(self):
        r = assign_fulltime_punches([self._at(13, 0), self._at(13, 4, 59)], time(12, 30))
        self.assertEqual(r, {'PM_IN': self._at(13, 0)})

    def test_exactly_five_minutes_is_a_real_out(self):
        r = assign_fulltime_punches([self._at(13, 0), self._at(13, 5)], time(12, 30))
        self.assertEqual(r['PM_OUT'], self._at(13, 5))

    def test_a_double_tap_doesnt_hide_the_real_out(self):
        r = assign_fulltime_punches([self._at(7, 55), self._at(7, 55, 30), self._at(12, 1)],
                                    time(12, 30))
        self.assertEqual((r['AM_IN'], r['AM_OUT']), (self._at(7, 55), self._at(12, 1)))

    def test_parttime_double_tap_is_incomplete_not_zero_hours_present(self):
        self.assertEqual(assign_parttime_punches([self._at(8, 0), self._at(8, 1)]),
                         {'IN': self._at(8, 0)})


class ForgotAmOutTests(TestCase):
    """Forgot to time out for the morning: the first punch after 12:30 (midpoint)
    and before 13:00 (PM IN) is the AM_OUT; the next one starts the PM."""

    def _at(self, h, m, s=0):
        return aware(WORKDAY, time(h, m, s))

    def _assign(self, *times):
        return assign_fulltime_punches(list(times), time(12, 30), am_out=time(12, 0),
                                       pm_out=time(17, 0), pm_in=time(13, 0))

    def test_two_lunch_punches_are_am_out_then_pm_in(self):
        r = self._assign(self._at(7, 55), self._at(12, 40), self._at(12, 42), self._at(17, 0))
        self.assertEqual(r, {'AM_IN': self._at(7, 55), 'AM_OUT': self._at(12, 40),
                             'PM_IN': self._at(12, 42), 'PM_OUT': self._at(17, 0)})

    def test_one_lunch_punch_is_the_am_out(self):
        r = self._assign(self._at(7, 55), self._at(12, 40))
        self.assertEqual(r, {'AM_IN': self._at(7, 55), 'AM_OUT': self._at(12, 40)})

    def test_within_a_minute_of_the_am_out_is_a_duplicate(self):
        r = self._assign(self._at(7, 55), self._at(12, 40), self._at(12, 40, 50))
        self.assertEqual(r, {'AM_IN': self._at(7, 55), 'AM_OUT': self._at(12, 40)})

    def test_a_minute_later_is_already_the_pm_in(self):
        r = self._assign(self._at(7, 55), self._at(12, 40), self._at(12, 41))
        self.assertEqual(r['PM_IN'], self._at(12, 41))

    def test_morning_already_timed_out_keeps_lunch_punch_as_pm_in(self):
        r = self._assign(self._at(7, 55), self._at(12, 0), self._at(12, 40))
        self.assertEqual((r['AM_OUT'], r['PM_IN']), (self._at(12, 0), self._at(12, 40)))

    def test_no_morning_at_all_keeps_lunch_punch_as_pm_in(self):
        r = self._assign(self._at(12, 45), self._at(17, 0))
        self.assertEqual(r, {'PM_IN': self._at(12, 45), 'PM_OUT': self._at(17, 0)})

    def test_punch_at_one_oclock_is_not_in_the_lunch_window(self):
        r = self._assign(self._at(7, 55), self._at(13, 0), self._at(17, 0))
        self.assertNotIn('AM_OUT', r)
        self.assertEqual(r['PM_IN'], self._at(13, 0))


class ForgotAmOutIngestTests(TestCase):
    """Live (one punch at a time) and sync (all at once) end up the same."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()   # 08:00-12:00 / 13:00-17:00, midpoint 12:30
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(employee_no='FO-1', first_name='Forgot', last_name='Out',
                                          department=dept, biometric_id='6701', is_fulltime=True)

    def _punch(self, *hms):
        return [RawPunch(biometric_id='6701', timestamp=aware(WORKDAY, time(*t)), status=0)
                for t in hms]

    def _types(self):
        return {timezone.localtime(l.log_datetime).strftime('%H:%M'): l.log_type
                for l in AttendanceLog.objects.filter(employee=self.emp)}

    EXPECTED = {'07:55': 'AM_IN', '12:40': 'AM_OUT', '12:42': 'PM_IN', '17:00': 'PM_OUT'}

    def test_live_one_punch_at_a_time(self):
        for t in [(7, 55), (12, 40), (12, 42), (17, 0)]:
            ingest_punches(self._punch(t), notify=False)
        self.assertEqual(self._types(), self.EXPECTED)
        self.assertFalse(Absence.objects.filter(employee=self.emp).exists())   # whole day present

    def test_sync_all_at_once(self):
        ingest_punches(self._punch((7, 55), (12, 40), (12, 42), (17, 0)), notify=False)
        self.assertEqual(self._types(), self.EXPECTED)

    def test_morning_in_arriving_late_relabels_the_lunch_punch(self):
        # The listener missed 07:55, so 12:40 was first stored as PM_IN. The
        # periodic sync re-reads the last 24h from the device (07:55 included);
        # 12:40 then becomes the AM_OUT - one row, not a PM_IN and an AM_OUT.
        for t in [(12, 40), (12, 42), (17, 0)]:
            ingest_punches(self._punch(t), notify=False)
        ingest_punches(self._punch((7, 55), (12, 40), (12, 42), (17, 0)), notify=False)
        self.assertEqual(self._types(), self.EXPECTED)
        self.assertEqual(AttendanceLog.objects.filter(employee=self.emp).count(), 4)


class DuplicatePunchIngestTests(TestCase):
    """End to end: the second tap arriving later (real-time listener) still
    doesn't become an OUT, and the session stays open/absent, not present."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(employee_no='DP-1', first_name='Double', last_name='Tap',
                                          department=dept, biometric_id='6601', is_fulltime=True)

    def test_second_tap_in_a_later_batch_is_ignored(self):
        ingest_punches([RawPunch(biometric_id='6601', timestamp=aware(WORKDAY, time(7, 55)),
                                 status=0)], notify=False)
        ingest_punches([RawPunch(biometric_id='6601', timestamp=aware(WORKDAY, time(7, 55, 20)),
                                 status=0)], notify=False)
        self.assertEqual(list(AttendanceLog.objects.filter(employee=self.emp)
                              .values_list('log_type', flat=True)), ['AM_IN'])
        # A past day with only the AM_IN: AM is absent (no OUT), not present.
        self.assertTrue(Absence.objects.filter(employee=self.emp, session='AM').exists())

    def test_exact_minutes(self):
        # 08:00 + grace 5 => 08:05 threshold.
        self.assertEqual(minutes_late(time(8, 5), time(8, 0), 5), 0)
        self.assertEqual(minutes_late(time(8, 20), time(8, 0), 5), 15)
        self.assertEqual(minutes_late(time(8, 6), time(8, 0), 5), 1)   # per-minute


class MinutesUndertimeTests(TestCase):
    def test_exact_minutes_no_grace(self):
        # Leaving before the scheduled OUT is counted exactly — grace is
        # an arrival-only allowance and must NOT forgive early departure.
        self.assertEqual(minutes_undertime(time(16, 57), time(17, 0)), 3)
        self.assertEqual(minutes_undertime(time(11, 45), time(12, 0)), 15)
        self.assertEqual(minutes_undertime(time(17, 0), time(17, 0)), 0)
        self.assertEqual(minutes_undertime(time(17, 30), time(17, 0)), 0)  # stayed late


class _FullTimeBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()  # defaults: am 08:00/12:00, pm 13:00/17:00, grace 5
        cls.dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='FT-1', first_name='Full', last_name='Time',
            department=cls.dept, biometric_id='3001', is_fulltime=True)

    def _log(self, log_type, t):
        return AttendanceLog.objects.create(
            employee=self.emp, log_type=log_type, log_datetime=aware(WORKDAY, t))


class FullTimeProcessTests(_FullTimeBase):
    def test_four_clean_punches_present(self):
        self._log('AM_IN', time(8, 1)); self._log('AM_OUT', time(12, 0))
        self._log('PM_IN', time(13, 1)); self._log('PM_OUT', time(17, 5))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'PRESENT')
        self.assertEqual(Lates.objects.count(), 0)
        self.assertEqual(Absence.objects.count(), 0)

    def test_late_am_exact_minutes(self):
        self._log('AM_IN', time(8, 20)); self._log('AM_OUT', time(12, 0))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(17, 0))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'LATE')
        late = Lates.objects.get(employee=self.emp, date=WORKDAY, session='AM')
        self.assertEqual(late.minutes_late, 15)

    def test_missing_pm_session_half_day(self):
        self._log('AM_IN', time(8, 0)); self._log('AM_OUT', time(12, 0))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'HALF_DAY')
        self.assertTrue(Absence.objects.filter(
            employee=self.emp, date=WORKDAY, session='PM').exists())

    def test_no_punches_absent(self):
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'ABSENT')
        self.assertEqual(Absence.objects.filter(date=WORKDAY).count(), 2)  # AM + PM

    def test_idempotent(self):
        self._log('AM_IN', time(8, 20)); self._log('AM_OUT', time(12, 0))
        process_day(self.emp, WORKDAY)
        process_day(self.emp, WORKDAY)
        self.assertEqual(Lates.objects.count(), 1)
        self.assertEqual(Absence.objects.filter(date=WORKDAY).count(), 1)  # PM only


class AfterHoursIngestionTests(_FullTimeBase):
    """End-to-end: two punches after PM_OUT must read as absent (missing IN),
    never as present-but-late — the actual scenario the rule exists for."""

    def test_two_after_hours_punches_are_absent_not_late(self):
        ingest_punches([
            RawPunch(biometric_id='3001', timestamp=aware(WORKDAY, time(17, 0))),
            RawPunch(biometric_id='3001', timestamp=aware(WORKDAY, time(18, 0))),
        ])
        self.assertFalse(AttendanceLog.objects.filter(log_type='PM_IN').exists())
        pm_out = AttendanceLog.objects.get(log_type='PM_OUT')
        self.assertEqual(timezone.localtime(pm_out.log_datetime).time(), time(18, 0))

        self.assertFalse(Lates.objects.exists())
        am_absence = Absence.objects.get(employee=self.emp, date=WORKDAY, session='AM')
        self.assertEqual(am_absence.reason, Absence.Reason.NO_PUNCH)
        pm_absence = Absence.objects.get(employee=self.emp, date=WORKDAY, session='PM')
        self.assertEqual(pm_absence.reason, Absence.Reason.MISSING_IN)
        self.assertTrue(pm_absence.incomplete)


class DeactivatedEmployeeIngestionTests(_FullTimeBase):
    """A deactivated employee's own fingerprint punches must never be recorded
    - ingest_punches only recognises active employees by biometric_id, so a
    deactivated badge reads as 'unknown' rather than attendance."""

    def test_punch_is_silently_skipped_once_deactivated(self):
        self.emp.is_active = False
        self.emp.save(update_fields=['is_active'])
        summary = ingest_punches([
            RawPunch(biometric_id='3001', timestamp=aware(WORKDAY, time(8, 0))),
        ])
        self.assertEqual(summary['created'], 0)
        self.assertEqual(summary['skipped'], 1)
        self.assertFalse(AttendanceLog.objects.exists())


class BackfillCommandTests(_FullTimeBase):
    """`backfill_attendance` fills in the derived rows for days the daily job
    never ran (stack down), without notifying anyone about old days."""

    SINCE, UNTIL = date(2026, 7, 6), date(2026, 7, 7)   # Mon, Tue

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Employee.objects.update(created_at=aware(date(2026, 6, 1), time(9, 0)))

    def _run(self, **kw):
        out = StringIO()
        call_command('backfill_attendance', stdout=out, **kw)
        return out.getvalue()

    @override_settings(ATTENDANCE_START_DATE='2026-07-01')
    def test_creates_absences_for_unprocessed_days_and_is_idempotent(self):
        self.assertEqual(Absence.objects.count(), 0)
        self._run(since='2026-07-06', until='2026-07-07')
        # 2 workdays x (AM + PM) for the one employee.
        self.assertEqual(Absence.objects.filter(employee=self.emp).count(), 4)
        self._run(since='2026-07-06', until='2026-07-07')
        self.assertEqual(Absence.objects.filter(employee=self.emp).count(), 4)

    @override_settings(ATTENDANCE_START_DATE='2026-07-01')
    def test_holiday_with_no_bridging_attendance_is_also_counted_absent(self):
        # No punches anywhere nearby, so the holiday's before/after bridging
        # exemption fails too - it's absent exactly like the surrounding
        # workday, not silently skipped (see holiday_requires_attendance).
        Holiday.objects.create(date=date(2026, 7, 7), name='Test Holiday')
        self._run(since='2026-07-06', until='2026-07-07')
        self.assertEqual(
            sorted(Absence.objects.values_list('date', flat=True).distinct()),
            [date(2026, 7, 6), date(2026, 7, 7)])

    @override_settings(ATTENDANCE_START_DATE='2026-07-01')
    def test_sends_no_notifications(self):
        self._run(since='2026-07-06', until='2026-07-07')
        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(ATTENDANCE_START_DATE='2026-07-01')
    def test_dry_run_changes_nothing(self):
        text = self._run(since='2026-07-06', until='2026-07-07', dry_run=True)
        self.assertIn('Would process 2 employee-day', text)
        self.assertEqual(Absence.objects.count(), 0)

    def test_never_before_the_employee_existed(self):
        Employee.objects.update(created_at=aware(date(2026, 7, 7), time(9, 0)))
        self._run(since='2026-07-06', until='2026-07-07')
        self.assertEqual(
            set(Absence.objects.values_list('date', flat=True)), {date(2026, 7, 7)})


class UndertimeProcessTests(_FullTimeBase):
    """Undertime = minutes left before the session's scheduled OUT (no grace)."""

    def test_early_pm_out_creates_undertime(self):
        self._log('AM_IN', time(8, 0)); self._log('AM_OUT', time(12, 0))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(16, 40))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['undertimes'], 1)
        u = Undertime.objects.get(employee=self.emp, date=WORKDAY, session='PM')
        self.assertEqual(u.minutes_undertime, 20)          # 17:00 - 16:40
        self.assertEqual(u.attendance_log.log_type, 'PM_OUT')

    def test_both_sessions_undertime(self):
        self._log('AM_IN', time(8, 0)); self._log('AM_OUT', time(11, 50))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(16, 55))
        process_day(self.emp, WORKDAY)
        self.assertEqual(Undertime.objects.count(), 2)
        total = sum(Undertime.objects.values_list('minutes_undertime', flat=True))
        self.assertEqual(total, 15)                        # 10 (AM) + 5 (PM)

    def test_grace_does_not_forgive_undertime(self):
        # Grace is 5 min. Leaving 3 min early must STILL count as 3 min undertime,
        # while arriving 3 min late is still forgiven.
        self._log('AM_IN', time(8, 3)); self._log('AM_OUT', time(12, 0))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(16, 57))
        process_day(self.emp, WORKDAY)
        self.assertEqual(Lates.objects.count(), 0)         # 3 min late < 5 grace
        self.assertEqual(Undertime.objects.get(session='PM').minutes_undertime, 3)

    def test_no_undertime_when_leaving_on_or_after_schedule(self):
        self._log('AM_IN', time(8, 0)); self._log('AM_OUT', time(12, 10))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(17, 0))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['undertimes'], 0)
        self.assertFalse(Undertime.objects.exists())

    def test_missing_out_is_absence_not_undertime(self):
        # No OUT punch => the session is an absence; it must not also be undertime.
        self._log('AM_IN', time(8, 0))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(17, 0))
        process_day(self.emp, WORKDAY)
        self.assertFalse(Undertime.objects.exists())
        self.assertTrue(Absence.objects.filter(session='AM').exists())

    def test_lost_time_is_late_plus_undertime(self):
        self._log('AM_IN', time(8, 20))    # 15 min late (grace 5)
        self._log('AM_OUT', time(12, 0))
        self._log('PM_IN', time(13, 0))
        self._log('PM_OUT', time(16, 50))  # 10 min undertime
        process_day(self.emp, WORKDAY)
        late = sum(Lates.objects.values_list('minutes_late', flat=True))
        under = sum(Undertime.objects.values_list('minutes_undertime', flat=True))
        self.assertEqual(late, 15)
        self.assertEqual(under, 10)
        self.assertEqual(late + under, 25)   # lost time

    def test_idempotent_no_duplicate_undertime(self):
        self._log('AM_IN', time(8, 0)); self._log('AM_OUT', time(12, 0))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(16, 40))
        process_day(self.emp, WORKDAY)
        process_day(self.emp, WORKDAY)
        self.assertEqual(Undertime.objects.count(), 1)


class OvertimeTests(_FullTimeBase):
    def _full_day(self):
        self._log('AM_IN', time(8, 0)); self._log('AM_OUT', time(12, 0))
        self._log('PM_IN', time(13, 0)); self._log('PM_OUT', time(17, 0))

    def test_authorized_overtime_computed(self):
        self._full_day()
        OTAuthorization.objects.create(employee=self.emp, date=WORKDAY, ot_start=time(17, 30))
        self._log('OT_IN', time(17, 40)); self._log('OT_OUT', time(19, 30))
        process_day(self.emp, WORKDAY)
        ot = Overtime.objects.get(employee=self.emp, date=WORKDAY)
        # OT_OUT - max(OT_IN, ot_start) = 19:30 - 17:40 = 110 min
        self.assertEqual(ot.minutes, 110)

    def test_ot_in_before_ot_start_clamped(self):
        self._full_day()
        OTAuthorization.objects.create(employee=self.emp, date=WORKDAY, ot_start=time(17, 30))
        self._log('OT_IN', time(17, 20))  # before ot_start -> clamp to 17:30
        self._log('OT_OUT', time(19, 30))
        process_day(self.emp, WORKDAY)
        ot = Overtime.objects.get(employee=self.emp, date=WORKDAY)
        self.assertEqual(ot.minutes, 120)  # 19:30 - 17:30

    def test_no_authorization_no_overtime(self):
        self._full_day()
        # Extra punches past pm_out, but NO authorization.
        self._log('OT_IN', time(18, 0)); self._log('OT_OUT', time(20, 0))
        process_day(self.emp, WORKDAY)
        self.assertEqual(Overtime.objects.count(), 0)

    def test_authorized_but_no_ot_punches(self):
        self._full_day()
        OTAuthorization.objects.create(employee=self.emp, date=WORKDAY, ot_start=time(17, 30))
        process_day(self.emp, WORKDAY)
        self.assertEqual(Overtime.objects.count(), 0)


class PartTimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        cls.dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='PT-1', first_name='Part', last_name='Time',
            department=cls.dept, biometric_id='3002', is_fulltime=False)

    def _log(self, log_type, t):
        return AttendanceLog.objects.create(
            employee=self.emp, log_type=log_type, log_datetime=aware(WORKDAY, t))

    def test_in_out_present(self):
        self._log('IN', time(8, 0)); self._log('OUT', time(17, 0))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'PRESENT')
        self.assertEqual(Absence.objects.count(), 0)

    def test_missing_out_is_incomplete_not_absent(self):
        self._log('IN', time(8, 0))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertFalse(Absence.objects.filter(employee=self.emp).exists())
        # The IN punch is retained.
        self.assertTrue(AttendanceLog.objects.filter(employee=self.emp, log_type='IN').exists())

    def test_no_punch_is_a_day_off(self):
        # Part-timers are judged on weekly hours, so a day without punches is no absence.
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'REST')
        self.assertFalse(Absence.objects.filter(employee=self.emp).exists())

    def test_never_late_or_overtime(self):
        self._log('IN', time(11, 0)); self._log('OUT', time(20, 0))  # very late, long day
        process_day(self.emp, WORKDAY)
        self.assertEqual(Lates.objects.count(), 0)
        self.assertEqual(Overtime.objects.count(), 0)

    def test_never_undertime(self):
        # Part-time has no fixed schedule, so an early OUT is not undertime.
        self._log('IN', time(8, 0)); self._log('OUT', time(10, 0))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['undertimes'], 0)
        self.assertEqual(Undertime.objects.count(), 0)


class SeedDemoAttendanceTests(TestCase):
    """`seed_demo_attendance`: realistic fake data that is identifiable, silent,
    never touches real records, and can be removed again."""

    MONTH = '2026-07'   # a fully completed month (23 weekdays, no holidays)

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        # 9001: late-leaver, 9004: chronically late, 9005: OT-eligible (see the mock).
        cls.people = [
            Employee.objects.create(
                employee_no=f'S-{bio}', first_name='S', last_name=bio, department=dept,
                biometric_id=bio, is_fulltime=fulltime)
            for bio, fulltime in (('9001', True), ('9004', True), ('9005', True), ('9007', False))
        ]
        # Long-standing staff, so days without punches are inferred absent.
        Employee.objects.update(created_at=aware(date(2026, 6, 1), time(9, 0)))

    def _seed(self, month=None, **kw):
        out = StringIO()
        call_command('seed_demo_attendance', month=month or self.MONTH, stdout=out, **kw)
        return out.getvalue()

    def _weekdays(self):
        day, out = date(2026, 7, 1), []
        while day.month == 7:
            if day.weekday() < 5:
                out.append(day)
            day += timedelta(days=1)
        return out

    def _day_logs(self, emp, day):
        return AttendanceLog.objects.filter(
            employee=emp,
            log_datetime__gte=aware(day, time.min),
            log_datetime__lt=aware(day + timedelta(days=1), time.min))

    # -- safety ----------------------------------------------------------------
    def test_every_employee_gets_demo_logs_and_nothing_is_notified(self):
        with patch('apps.notifications.services.notify_for_log') as notify:
            self._seed()
        notify.assert_not_called()
        self.assertEqual(Notification.objects.count(), 0)
        for emp in self.people:
            sources = set(AttendanceLog.objects.filter(employee=emp)
                          .values_list('source', flat=True))
            self.assertEqual(sources, {'DEMO'}, emp.employee_no)

    def test_real_punch_days_are_left_alone(self):
        who, day = self.people[0], date(2026, 7, 8)
        real = AttendanceLog.objects.create(
            employee=who, log_type='AM_IN', log_datetime=aware(day, time(8, 1)),
            source=AttendanceLog.Source.DEVICE)
        self._seed()
        logs = list(self._day_logs(who, day))
        self.assertEqual([l.pk for l in logs], [real.pk])
        # ...while the rest of that employee's month is filled in.
        self.assertTrue(AttendanceLog.objects.filter(employee=who, source='DEMO').exists())

    def test_admin_created_ot_authorization_day_is_left_alone(self):
        who, day = self.people[2], date(2026, 7, 8)          # 9005 is OT-eligible
        OTAuthorization.objects.create(employee=who, date=day, ot_start=time(18, 0),
                                       note='Board work')
        self._seed()
        self.assertFalse(self._day_logs(who, day).exists())
        self.assertEqual(OTAuthorization.objects.get(employee=who, date=day).note, 'Board work')

    def test_ot_authorizations_are_labelled_demo(self):
        self._seed()
        self.assertFalse(OTAuthorization.objects.exclude(note='Demo data').exists())
        self.assertTrue(OTAuthorization.objects.filter(
            note='Demo data', employee__biometric_id='9005').exists())

    # -- coverage ---------------------------------------------------------------
    def test_every_workday_is_accounted_for(self):
        self._seed()
        for emp in self.people:
            logged = {timezone.localtime(l.log_datetime).date()
                      for l in AttendanceLog.objects.filter(employee=emp)}
            absent = set(Absence.objects.filter(employee=emp).values_list('date', flat=True))
            for day in self._weekdays():
                self.assertTrue(day in logged or day in absent, f'{emp.employee_no} {day}')

    def test_rerun_replaces_instead_of_duplicating(self):
        def snapshot():
            return sorted(AttendanceLog.objects.values_list(
                'employee_id', 'log_datetime', 'log_type', 'source'))
        self._seed()
        first = snapshot()
        self.assertTrue(first)
        self._seed()
        self.assertEqual(snapshot(), first)

    def test_future_month_seeds_nothing(self):
        text = self._seed(month='2099-01')
        self.assertIn('Nothing to seed', text)
        self.assertFalse(AttendanceLog.objects.exists())

    def test_bad_month_rejected(self):
        with self.assertRaises(CommandError):
            self._seed(month='July')

    # -- removal ---------------------------------------------------------------
    def test_clear_removes_demo_rows_and_keeps_real_data(self):
        who, day = self.people[0], date(2026, 7, 8)
        real = AttendanceLog.objects.create(
            employee=who, log_type='AM_IN', log_datetime=aware(day, time(8, 1)),
            source=AttendanceLog.Source.DEVICE)
        self._seed()
        self.assertTrue(AttendanceLog.objects.filter(source='DEMO').exists())
        self._seed(clear=True)

        self.assertFalse(AttendanceLog.objects.filter(source='DEMO').exists())
        self.assertFalse(OTAuthorization.objects.filter(note='Demo data').exists())
        self.assertTrue(AttendanceLog.objects.filter(pk=real.pk).exists())
        # Nothing fake is left behind for days before real tracking began.
        self.assertFalse(Absence.objects.filter(date__lt=day).exists())
        self.assertFalse(Lates.objects.filter(date__lt=day).exists())

    # -- the reports, checked against the raw punches -----------------------------
    def _expected(self, emp):
        """Independent recount straight from the punches (no selectors/reports):
        (both_sessions_days, half_days, absent_days, late_minutes)."""
        by_day = defaultdict(dict)
        for log in AttendanceLog.objects.filter(employee=emp):
            local = timezone.localtime(log.log_datetime)
            by_day[local.date()][log.log_type] = local.hour * 60 + local.minute
        both = half = absent = late = 0
        for day in self._weekdays():
            t = by_day.get(day, {})
            am = 'AM_IN' in t and 'AM_OUT' in t
            pm = 'PM_IN' in t and 'PM_OUT' in t
            both += int(am and pm)
            half += int(am != pm)
            absent += int(not am and not pm)
            if am:
                late += max(0, t['AM_IN'] - (8 * 60 + 5))     # 08:00 + 5 min grace
            if pm:
                late += max(0, t['PM_IN'] - (13 * 60 + 5))    # 13:00 + 5 min grace
        return both, half, absent, late

    def test_reports_agree_with_the_raw_punches(self):
        from apps.api.selectors import monthly_summary
        from apps.reports.generators import build_report
        from apps.reports.tests import xlsx_rows

        self._seed()
        fulltime = [e for e in self.people if e.is_fulltime]
        all_expected = [self._expected(e) for e in fulltime]
        # The dataset must actually exercise the interesting cases, or this proves little.
        self.assertTrue(any(h for _, h, _, _ in all_expected), 'no half-days seeded')
        self.assertTrue(any(a for _, _, a, _ in all_expected), 'no absences seeded')
        self.assertTrue(any(m for _, _, _, m in all_expected), 'no lateness seeded')

        _, tardiness = build_report('TARDINESS', 'XLSX', {'start': '2026-07-01', 'end': '2026-07-31'})
        tardiness_rows = xlsx_rows(tardiness)[1:]

        for emp, (both, half, absent, late) in zip(fulltime, all_expected):
            label = emp.employee_no
            # 1) the API/portal summary
            s = monthly_summary(emp, 2026, 7)
            self.assertEqual(s['present'] + s['late'], both, label)
            self.assertEqual(s['half_day'], half, label)
            self.assertEqual(s['absent'], absent, label)
            self.assertEqual(s['late_minutes'], late, label)
            # 2) the Attendance report's rows
            _, att = build_report('ATTENDANCE', 'XLSX', {
                'employee': emp.id, 'start': '2026-07-01', 'end': '2026-07-31'})
            att_rows = [r for r in xlsx_rows(att)[1:] if r[0] != 'TOTAL']
            statuses = [r[6] for r in att_rows]
            self.assertEqual(statuses.count('PRESENT') + statuses.count('LATE'), both, label)
            self.assertEqual(statuses.count('HALF DAY'), half, label)
            self.assertEqual(statuses.count('ABSENT'), absent, label)
            self.assertEqual(sum(r[7] for r in att_rows), late, label)
            # 3) the Tardiness report (sum of this employee's rows)
            self.assertEqual(sum(r[5] for r in tardiness_rows if r[1] == emp.employee_no), late, label)


class MockClientDefaultsTests(TestCase):
    def test_defaults_keep_the_original_live_simulator_behaviour(self):
        from apps.devices.clients.mock_client import MockDeviceClient
        client = MockDeviceClient()
        self.assertEqual(client.ot_note, 'auto (mock simulator)')
        self.assertIsNone(client.until)
        self.assertEqual(client.skip, set())
