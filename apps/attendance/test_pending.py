"""A session is not judged until its scheduled time out has been reached: before
then, a lone IN (or nothing yet) is 'in progress', never absent / missing a punch."""

from datetime import date, datetime, time
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.api.selectors import build_daily_attendance, monthly_summary
from apps.attendance.models import Absence, AttendanceLog
from apps.attendance.services import process_day, session_is_open
from apps.attendance.tasks import process_daily_attendance_task
from apps.notifications.models import Notification
from apps.organization.models import Department, Employee, GlobalSchedule

TODAY = date(2026, 7, 6)   # a Monday; default schedule 08-12 / 13-17


def at(t, d=TODAY):
    return timezone.make_aware(datetime.combine(d, t))


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.full = Employee.objects.create(
            employee_no='PD-1', first_name='Full', last_name='Timer',
            department=dept, biometric_id='7001', is_fulltime=True)
        cls.part = Employee.objects.create(
            employee_no='PD-2', first_name='Part', last_name='Timer',
            department=dept, biometric_id='7002', is_fulltime=False)

    def _log(self, emp, log_type, t, d=TODAY):
        return AttendanceLog.objects.create(
            employee=emp, log_type=log_type, log_datetime=at(t, d))

    def clock(self, t, d=TODAY):
        """Pretend it is `t` on day `d`."""
        return patch('django.utils.timezone.now', return_value=at(t, d))


class SessionIsOpenTests(TestCase):
    def test_boundaries(self):
        out = time(12, 0)
        self.assertTrue(session_is_open(TODAY, out, now=at(time(9, 0))))
        self.assertTrue(session_is_open(TODAY, out, now=at(time(11, 59))))
        self.assertFalse(session_is_open(TODAY, out, now=at(time(12, 0))))   # reached
        self.assertFalse(session_is_open(TODAY, out, now=at(time(15, 0))))

    def test_other_days(self):
        out = time(12, 0)
        self.assertFalse(session_is_open(date(2026, 7, 3), out, now=at(time(9, 0))))  # past
        self.assertTrue(session_is_open(date(2026, 7, 7), out, now=at(time(20, 0))))  # future


class ProcessDayPendingTests(_Base):
    def test_lone_am_in_before_am_out_is_not_absent(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        result = process_day(self.full, TODAY, now=at(time(9, 30)))
        self.assertEqual(result['status'], 'PENDING')
        self.assertFalse(Absence.objects.exists())

    def test_nothing_yet_is_not_absent_either(self):
        result = process_day(self.full, TODAY, now=at(time(8, 45)))
        self.assertEqual(result['status'], 'PENDING')
        self.assertFalse(Absence.objects.exists())

    def test_am_reached_pm_still_open_only_judges_am(self):
        # 14:00: AM's time out passed with no OUT -> AM is missing; PM (out 17:00) is open.
        self._log(self.full, 'AM_IN', time(8, 0))
        result = process_day(self.full, TODAY, now=at(time(14, 0)))
        self.assertEqual(result['status'], 'PENDING')
        rows = list(Absence.objects.values_list('session', 'reason'))
        self.assertEqual(rows, [('AM', Absence.Reason.MISSING_OUT)])

    def test_completed_am_and_open_pm(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        self._log(self.full, 'AM_OUT', time(12, 0))
        result = process_day(self.full, TODAY, now=at(time(13, 30)))
        self.assertEqual(result['status'], 'PENDING')
        self.assertFalse(Absence.objects.exists())

    def test_time_out_reached_decides_as_before(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        self._log(self.full, 'AM_OUT', time(12, 0))
        result = process_day(self.full, TODAY, now=at(time(17, 0)))
        self.assertEqual(result['status'], 'HALF_DAY')
        self.assertEqual(list(Absence.objects.values_list('session', flat=True)), ['PM'])

    def test_later_run_replaces_the_pending_state(self):
        # Idempotent: the same day re-processed after the time out gets decided.
        self._log(self.full, 'AM_IN', time(8, 0))
        process_day(self.full, TODAY, now=at(time(9, 0)))
        self.assertFalse(Absence.objects.exists())
        result = process_day(self.full, TODAY, now=at(time(18, 0)))
        self.assertEqual(result['status'], 'ABSENT')
        self.assertEqual(Absence.objects.count(), 2)

    def test_a_past_day_is_always_decided(self):
        result = process_day(self.full, date(2026, 7, 3), now=at(time(9, 0)))
        self.assertEqual(result['status'], 'ABSENT')

    def test_parttime_undecided_until_end_of_day(self):
        self._log(self.part, 'IN', time(8, 0))
        result = process_day(self.part, TODAY, now=at(time(10, 0)))
        self.assertEqual(result['status'], 'PENDING')
        self.assertFalse(Absence.objects.exists())
        result = process_day(self.part, TODAY, now=at(time(17, 30)))
        self.assertEqual(result['status'], 'ABSENT')
        self.assertTrue(Absence.objects.get().incomplete)

    def test_parttime_complete_is_present_at_any_time(self):
        self._log(self.part, 'IN', time(8, 0))
        self._log(self.part, 'OUT', time(9, 0))
        self.assertEqual(process_day(self.part, TODAY, now=at(time(9, 5)))['status'], 'PRESENT')


class ApiPendingTests(_Base):
    def _today(self, emp):
        return build_daily_attendance(emp, TODAY, TODAY)[0]

    def test_full_time_morning_with_only_am_in(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        with self.clock(time(9, 30)):
            rec = self._today(self.full)
        self.assertEqual(rec['am']['status'], 'PENDING')
        self.assertEqual(rec['pm']['status'], 'PENDING')
        self.assertEqual(rec['day_status'], 'PENDING')

    def test_full_time_after_am_out_missing_is_absent_am_only(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        with self.clock(time(14, 0)):
            rec = self._today(self.full)
        self.assertEqual(rec['am']['status'], 'ABSENT')
        self.assertEqual(rec['pm']['status'], 'PENDING')
        self.assertEqual(rec['day_status'], 'PENDING')

    def test_end_of_day_decides(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        self._log(self.full, 'AM_OUT', time(12, 0))
        with self.clock(time(17, 1)):
            rec = self._today(self.full)
        self.assertEqual(rec['day_status'], 'HALF_DAY')

    def test_past_days_unchanged(self):
        self._log(self.full, 'AM_IN', time(8, 0))          # a lone IN, but two days ago
        with self.clock(time(9, 0), date(2026, 7, 8)):
            rec = build_daily_attendance(self.full, TODAY, TODAY)[0]
        self.assertEqual(rec['am']['status'], 'ABSENT')
        self.assertEqual(rec['day_status'], 'ABSENT')

    def test_part_time(self):
        self._log(self.part, 'IN', time(8, 0))
        with self.clock(time(10, 0)):
            rec = self._today(self.part)
        self.assertEqual(rec['day_status'], 'PENDING')
        self.assertFalse(rec['incomplete'])
        with self.clock(time(17, 30)):
            rec = self._today(self.part)
        self.assertEqual(rec['day_status'], 'ABSENT')
        self.assertEqual(rec['missing'], 'OUT')

    def test_monthly_summary_does_not_count_an_open_day(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        with self.clock(time(9, 30)):
            s = monthly_summary(self.full, 2026, 7)
        self.assertEqual(s['absent'], 0)


class DailyJobPendingTests(_Base):
    def test_no_absence_notification_while_the_day_is_in_progress(self):
        self._log(self.full, 'AM_IN', time(8, 0))
        with self.clock(time(9, 30)):
            summary = process_daily_attendance_task(TODAY.isoformat())
        self.assertEqual(summary['absences'], 0)
        self.assertFalse(Notification.objects.filter(type='ABSENCE').exists())

    def test_notifies_once_the_time_out_has_passed(self):
        with self.clock(time(12, 5)):
            process_daily_attendance_task(TODAY.isoformat())
        sessions = set(Absence.objects.filter(employee=self.full).values_list('session', flat=True))
        self.assertEqual(sessions, {'AM'})
        self.assertTrue(Notification.objects.filter(employee=self.full, type='ABSENCE').exists())
