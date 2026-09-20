"""Phase 8 tests for the daily-processing Celery task."""

from datetime import date, datetime, time

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.attendance.models import Absence, AttendanceLog, Lates
from apps.attendance.tasks import process_daily_attendance_task
from apps.notifications.models import Notification
from apps.organization.models import Department, Employee, GlobalSchedule, Holiday

WORKDAY = date(2026, 7, 6)   # Monday
SUNDAY = date(2026, 7, 5)


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, FIREBASE_CREDENTIALS='')
class ProcessDailyAttendanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.late = cls._emp(dept, 'L-1', '5001')
        cls.absent = cls._emp(dept, 'A-1', '5002')

        # Late employee: full 4 punches, AM 20 min late.
        for lt, t in [('AM_IN', time(8, 25)), ('AM_OUT', time(12, 0)),
                      ('PM_IN', time(13, 0)), ('PM_OUT', time(17, 0))]:
            AttendanceLog.objects.create(employee=cls.late, log_type=lt,
                                         log_datetime=aware(WORKDAY, t))
        # Absent employee: no punches.

    @staticmethod
    def _emp(dept, no, bio):
        return Employee.objects.create(
            employee_no=no, first_name=no, last_name='X',
            department=dept, biometric_id=bio, is_fulltime=True)

    def test_flags_and_notifies(self):
        summary = process_daily_attendance_task(WORKDAY.isoformat())
        self.assertTrue(Lates.objects.filter(employee=self.late, session='AM').exists())
        self.assertEqual(Absence.objects.filter(employee=self.absent, date=WORKDAY).count(), 2)
        # Absence is notified by the daily task...
        self.assertTrue(Notification.objects.filter(employee=self.absent, type='ABSENCE').exists())
        # ...but lateness is NOT (Phase 10: it rides on the immediate per-punch push).
        self.assertFalse(Notification.objects.filter(type='LATE').exists())
        self.assertGreater(summary['notifications'], 0)

    def test_idempotent(self):
        process_daily_attendance_task(WORKDAY.isoformat())
        process_daily_attendance_task(WORKDAY.isoformat())
        self.assertEqual(Lates.objects.filter(employee=self.late).count(), 1)
        # Two session absences (AM+PM) for the absent full-timer, not duplicated
        # on re-run.
        self.assertEqual(Notification.objects.filter(type='ABSENCE').count(), 2)

    def test_skips_holiday(self):
        Holiday.objects.create(date=WORKDAY, name='Holiday')
        process_daily_attendance_task(WORKDAY.isoformat())
        self.assertFalse(Absence.objects.filter(date=WORKDAY).exists())

    def test_skips_non_workday(self):
        process_daily_attendance_task(SUNDAY.isoformat())
        self.assertFalse(Absence.objects.filter(date=SUNDAY).exists())
