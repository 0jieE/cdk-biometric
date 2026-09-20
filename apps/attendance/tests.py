"""Phase 8 attendance logic tests."""

from datetime import date, datetime, time

from django.test import TestCase
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
from apps.organization.models import Department, Employee, GlobalSchedule

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


class MinutesLateTests(TestCase):
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

    def test_missing_out_incomplete_absent(self):
        self._log('IN', time(8, 0))
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'ABSENT')
        ab = Absence.objects.get(employee=self.emp, date=WORKDAY)
        self.assertTrue(ab.incomplete)
        self.assertEqual(ab.reason, Absence.Reason.MISSING_OUT)
        self.assertEqual(ab.session, 'FULL')
        # The IN punch is retained.
        self.assertTrue(AttendanceLog.objects.filter(employee=self.emp, log_type='IN').exists())

    def test_no_punch_plain_absent(self):
        result = process_day(self.emp, WORKDAY)
        self.assertEqual(result['status'], 'ABSENT')
        ab = Absence.objects.get(employee=self.emp, date=WORKDAY)
        self.assertFalse(ab.incomplete)
        self.assertEqual(ab.reason, Absence.Reason.NO_PUNCH)

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
