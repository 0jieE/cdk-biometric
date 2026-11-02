"""Holidays no longer silently skip attendance.

A holiday normally needs no punch (not absent even with none). But if the
employee was ALSO absent on the nearest scheduled workday right before OR
right after it, the holiday itself starts requiring attendance too - this is
the "bridging" rule that catches an unauthorized leave padded onto a holiday.
If they actually punch in on a holiday, it must show as a real day (present
or late), never be hidden. Sunday is never a workday; Saturday follows
whatever the schedule says - both unchanged by this feature.
"""

from datetime import date, datetime, time
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.api.selectors import build_daily_attendance
from apps.attendance.models import Absence, AttendanceLog
from apps.attendance.services import (
    _nearest_workday,
    holiday_requires_attendance,
    process_day,
)
from apps.attendance.tasks import process_daily_attendance_task
from apps.notifications.models import Notification
from apps.organization.models import Department, Employee, GlobalSchedule, Holiday
from apps.organization.schedule import get_effective_schedule

MON = date(2026, 7, 6)
TUE = date(2026, 7, 7)    # the holiday
WED = date(2026, 7, 8)
THU = date(2026, 7, 9)
FRI = date(2026, 7, 10)
SAT = date(2026, 7, 11)
SUN = date(2026, 7, 12)


def at(t, d=TUE):
    return timezone.make_aware(datetime.combine(d, t))


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()  # Mon-Fri, 08-12 / 13-17
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='HB-1', first_name='Holiday', last_name='Bridge',
            department=dept, biometric_id='9701', is_fulltime=True)
        # Backdate: tracking_start() floors inference at the employee's own
        # created_at, which defaults to the real test-run clock (today),
        # well after these July 2026 fixture dates.
        Employee.objects.filter(pk=cls.emp.pk).update(created_at=at(time(9, 0), date(2026, 6, 1)))
        cls.emp.refresh_from_db()
        cls.holiday = Holiday.objects.create(date=TUE, name='Test Holiday')

    def _punch(self, d, log_type='AM_IN', t=time(8, 0)):
        return AttendanceLog.objects.create(
            employee=self.emp, log_type=log_type, log_datetime=at(t, d))

    def _as_of(self, d):
        """Pretend 'today' is `d` from holiday_requires_attendance's point of
        view - these are all historical fixture dates, so without this the
        real wall-clock 'today' would make every day in the past 'knowable'
        and the provisional-exemption behaviour couldn't be exercised."""
        return patch('apps.attendance.services.trusted_localdate', return_value=d)


class NearestWorkdayTests(TestCase):
    def test_skips_weekend_both_directions(self):
        workdays = [0, 1, 2, 3, 4]  # Mon-Fri
        self.assertEqual(_nearest_workday(SAT, workdays, 1), date(2026, 7, 13))  # next Monday
        self.assertEqual(_nearest_workday(SUN, workdays, -1), FRI)

    def test_skips_an_unscheduled_saturday(self):
        self.assertEqual(_nearest_workday(FRI, [0, 1, 2, 3, 4], 1), date(2026, 7, 13))  # next Monday
        self.assertEqual(_nearest_workday(FRI, [0, 1, 2, 3, 4, 5], 1), SAT)  # Saturday scheduled

    def test_none_when_no_workdays_at_all(self):
        self.assertIsNone(_nearest_workday(TUE, [], 1, max_days=5))


class HolidayRequiresAttendanceTests(_Base):
    def _sched(self):
        return get_effective_schedule(self.emp)

    def test_exempt_when_present_both_sides(self):
        self._punch(MON)
        self._punch(WED)
        self.assertFalse(holiday_requires_attendance(self.emp, self._sched(), TUE, today=THU))

    def test_absent_before_triggers_it(self):
        self._punch(WED)  # present after, absent before
        self.assertTrue(holiday_requires_attendance(self.emp, self._sched(), TUE, today=THU))

    def test_absent_after_triggers_it(self):
        self._punch(MON)  # present before, absent after
        self.assertTrue(holiday_requires_attendance(self.emp, self._sched(), TUE, today=THU))

    def test_absent_both_sides_triggers_it(self):
        self.assertTrue(holiday_requires_attendance(self.emp, self._sched(), TUE, today=THU))

    def test_future_after_day_gets_benefit_of_the_doubt(self):
        self._punch(MON)  # present before; WED hasn't happened yet
        self.assertFalse(holiday_requires_attendance(self.emp, self._sched(), TUE, today=MON))

    def test_weekend_is_skipped_when_finding_the_nearest_workday(self):
        # A holiday on Friday: "after" should look past Sat/Sun to Monday.
        fri_holiday = date(2026, 7, 10)
        self._punch(THU)                       # present the day before
        self._punch(date(2026, 7, 13))         # present the following Monday
        self.assertFalse(holiday_requires_attendance(self.emp, self._sched(), fri_holiday,
                                                      today=date(2026, 7, 13)))


class ProcessDayHolidayTests(_Base):
    def test_no_punch_and_bridged_is_exempt_rest(self):
        self._punch(MON)
        self._punch(WED)
        result = process_day(self.emp, TUE, now=at(time(9, 0), THU))
        self.assertEqual(result['status'], 'REST')
        self.assertFalse(Absence.objects.filter(date=TUE).exists())

    def test_no_punch_and_not_bridged_is_absent(self):
        # Nobody punched Monday either - bridging fails, holiday is judged.
        result = process_day(self.emp, TUE, now=at(time(9, 0), THU))
        self.assertEqual(result['status'], 'ABSENT')
        self.assertEqual(Absence.objects.filter(date=TUE).count(), 2)  # AM + PM

    def test_real_punches_on_a_holiday_are_judged_normally(self):
        # They actually came in and worked the holiday: must show as present,
        # never silently hidden/ignored just because it's a holiday.
        self._punch(TUE, 'AM_IN', time(8, 0))
        self._punch(TUE, 'AM_OUT', time(12, 0))
        self._punch(TUE, 'PM_IN', time(13, 0))
        self._punch(TUE, 'PM_OUT', time(17, 0))
        result = process_day(self.emp, TUE, now=at(time(18, 0)))
        self.assertEqual(result['status'], 'PRESENT')
        self.assertFalse(Absence.objects.filter(date=TUE).exists())

    def test_late_punch_on_a_holiday_is_still_late(self):
        self._punch(TUE, 'AM_IN', time(8, 20))
        self._punch(TUE, 'AM_OUT', time(12, 0))
        self._punch(TUE, 'PM_IN', time(13, 0))
        self._punch(TUE, 'PM_OUT', time(17, 0))
        result = process_day(self.emp, TUE, now=at(time(18, 0)))
        self.assertEqual(result['status'], 'LATE')

    def test_idempotent(self):
        self._punch(MON)
        self._punch(WED)
        process_day(self.emp, TUE, now=at(time(9, 0), THU))
        process_day(self.emp, TUE, now=at(time(9, 0), THU))
        self.assertFalse(Absence.objects.filter(date=TUE).exists())

    def test_sunday_is_never_a_workday_regardless_of_holidays(self):
        result = process_day(self.emp, SUN)
        self.assertEqual(result['status'], 'REST')
        self.assertFalse(Absence.objects.filter(date=SUN).exists())

    def test_saturday_follows_the_schedule_not_the_holiday_rule(self):
        # Default schedule is Mon-Fri -> Saturday is REST regardless.
        result = process_day(self.emp, SAT)
        self.assertEqual(result['status'], 'REST')
        self.assertFalse(Absence.objects.filter(date=SAT).exists())


class BuildDailyAttendanceHolidayTests(_Base):
    def test_exempt_holiday_is_hidden_like_a_weekend(self):
        self._punch(MON)
        self._punch(WED)
        process_day(self.emp, MON)
        process_day(self.emp, WED)
        with patch('apps.api.selectors.trusted_localdate', return_value=THU):
            recs = build_daily_attendance(self.emp, TUE, TUE)
        self.assertEqual(recs, [])

    def test_bridging_failure_shows_absent(self):
        # Absences are only inferred once the system has started recording
        # (its first punch); with no punch anywhere that'd be "today". An
        # unrelated June punch puts July in range.
        other = Employee.objects.create(
            employee_no='HB-2', first_name='Other', last_name='Person',
            department=self.emp.department, biometric_id='9702', is_fulltime=True)
        AttendanceLog.objects.create(employee=other, log_type='AM_IN',
                                     log_datetime=at(time(8, 0), date(2026, 6, 1)))
        with patch('apps.api.selectors.trusted_localdate', return_value=THU):
            recs = build_daily_attendance(self.emp, TUE, TUE)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['day_status'], 'ABSENT')

    def test_real_holiday_punches_show_present(self):
        self._punch(TUE, 'AM_IN', time(8, 0))
        self._punch(TUE, 'AM_OUT', time(12, 0))
        self._punch(TUE, 'PM_IN', time(13, 0))
        self._punch(TUE, 'PM_OUT', time(17, 0))
        with patch('apps.api.selectors.trusted_localdate', return_value=THU):
            recs = build_daily_attendance(self.emp, TUE, TUE)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['day_status'], 'PRESENT')


class DailyTaskHolidayTests(_Base):
    """The whole-day holiday skip is gone; recent holidays get re-settled
    once the 'day after' has actually happened."""

    def test_holiday_is_processed_not_skipped(self):
        summary = process_daily_attendance_task(TUE.isoformat())
        self.assertNotIn('skipped', summary)
        # No bridging data at all -> absent, same as any other workday.
        self.assertTrue(Absence.objects.filter(date=TUE).exists())

    def test_present_before_and_after_exempts_the_holiday(self):
        self._punch(MON)
        with self._as_of(MON):
            process_daily_attendance_task(MON.isoformat())
        with self._as_of(TUE):
            process_daily_attendance_task(TUE.isoformat())   # "after" not knowable yet -> exempt for now
        self._punch(WED)
        with self._as_of(WED):
            process_daily_attendance_task(WED.isoformat())   # re-settles TUE: now fully bridged
        self.assertFalse(Absence.objects.filter(date=TUE).exists())

    def test_turns_out_absent_after_once_the_day_after_happens(self):
        self._punch(MON)
        with self._as_of(MON):
            process_daily_attendance_task(MON.isoformat())
        with self._as_of(TUE):
            process_daily_attendance_task(TUE.isoformat())   # provisionally exempt
        self.assertFalse(Absence.objects.filter(date=TUE).exists())
        # Wednesday comes and goes with no punch -> re-settling TUE now fails bridging.
        with self._as_of(WED):
            process_daily_attendance_task(WED.isoformat())
        self.assertTrue(Absence.objects.filter(date=TUE).exists())
        self.assertTrue(Notification.objects.filter(
            employee=self.emp, type='ABSENCE', body__contains='2026-07-07').exists())

    def test_resettlement_does_not_double_notify(self):
        self._punch(MON)
        with self._as_of(MON):
            process_daily_attendance_task(MON.isoformat())
        with self._as_of(TUE):
            process_daily_attendance_task(TUE.isoformat())
        with self._as_of(WED):
            process_daily_attendance_task(WED.isoformat())   # TUE settles absent: AM + PM each notify once
        after_first_settlement = Notification.objects.filter(
            employee=self.emp, type='ABSENCE', body__contains='2026-07-07').count()
        self.assertEqual(after_first_settlement, 2)

        with self._as_of(THU):
            process_daily_attendance_task(THU.isoformat())   # TUE reprocessed again (still in the 7-day window)
        after_second_pass = Notification.objects.filter(
            employee=self.emp, type='ABSENCE', body__contains='2026-07-07').count()
        self.assertEqual(after_second_pass, 2)   # unchanged - no duplicates from reprocessing
