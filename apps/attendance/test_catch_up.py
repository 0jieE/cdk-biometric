"""Days the stack was down for still get their absences/lates.

The live job only processes *today*, so before catch-up existed any day with
no sync at all (server off, Docker stopped) never got its Absence rows - the
Attendance page (which infers from punches) showed those absences, while the
Absences page, lost-time totals and reports (which read the rows) showed none.
"""

from datetime import date, datetime, time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.attendance.models import Absence, AttendanceLog, ProcessingCursor
from apps.attendance.tasks import catch_up_finalized_days, sync_attendance_task
from apps.notifications.models import Notification
from apps.organization.models import Department, Employee, GlobalSchedule

MON, TUE, WED, THU, FRI, SAT = (date(2026, 7, d) for d in range(6, 12))


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, FIREBASE_CREDENTIALS='')
class CatchUpTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()   # Mon-Fri, 08-12 / 13-17
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='CU-1', first_name='Catch', last_name='Up',
            department=dept, biometric_id='9801', is_fulltime=True)
        Employee.objects.filter(pk=cls.emp.pk).update(created_at=aware(date(2026, 6, 1), time(0)))
        # The system's first punch ever (Mon AM only) -> tracking starts Monday.
        for lt, t in (('AM_IN', time(8, 0)), ('AM_OUT', time(12, 0))):
            AttendanceLog.objects.create(employee=cls.emp, log_type=lt, log_datetime=aware(MON, t))

    def _absent_dates(self):
        return sorted(set(Absence.objects.filter(employee=self.emp).values_list('date', flat=True)))

    def test_first_run_fills_every_past_day_since_tracking_began(self):
        result = catch_up_finalized_days(today=SAT)
        self.assertEqual(result['days'], 5)                      # Mon..Fri
        self.assertEqual(self._absent_dates(), [MON, TUE, WED, THU, FRI])
        self.assertEqual(Absence.objects.get(employee=self.emp, date=MON).session, 'PM')
        self.assertEqual(ProcessingCursor.load().last_finalized_date, FRI)

    def test_does_nothing_once_caught_up(self):
        catch_up_finalized_days(today=SAT)
        self.assertEqual(catch_up_finalized_days(today=SAT), {'days': 0})

    def test_resumes_after_an_outage_from_where_it_left_off(self):
        cursor = ProcessingCursor.load()
        cursor.last_finalized_date = TUE
        cursor.save()
        result = catch_up_finalized_days(today=SAT)
        self.assertEqual(result['days'], 3)                      # Wed..Fri only
        self.assertEqual(self._absent_dates(), [WED, THU, FRI])

    def test_only_yesterday_is_notified(self):
        catch_up_finalized_days(today=SAT)
        bodies = list(Notification.objects.filter(employee=self.emp, type='ABSENCE')
                      .values_list('body', flat=True))
        self.assertEqual(len(bodies), 2)                         # Fri AM + PM
        self.assertTrue(all('2026-07-10' in b for b in bodies))

    def test_never_before_the_employee_was_added(self):
        late = Employee.objects.create(
            employee_no='CU-2', first_name='New', last_name='Hire',
            department=self.emp.department, biometric_id='9802', is_fulltime=True)
        Employee.objects.filter(pk=late.pk).update(created_at=aware(THU, time(9, 0)))
        catch_up_finalized_days(today=SAT)
        self.assertEqual(
            sorted(set(Absence.objects.filter(employee=late).values_list('date', flat=True))),
            [THU, FRI])

    def test_each_sync_runs_the_catch_up(self):
        with patch('apps.attendance.tasks.refresh_offset'), \
             patch('apps.attendance.tasks.sync_attendance', return_value={}), \
             patch('apps.attendance.tasks.trusted_localdate', return_value=SAT):
            sync_attendance_task()
        self.assertEqual(ProcessingCursor.load().last_finalized_date, FRI)
        self.assertIn(THU, self._absent_dates())
