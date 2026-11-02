"""``regenerate_attendance``: stored punches are brought in line with the
current rules, and derived rows are re-computed."""

from datetime import date, datetime, time, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.attendance.models import Absence, AttendanceLog, Lates
from apps.organization.models import Department, Employee

DAY = date(2026, 10, 20)   # a Tuesday


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class RegenerateAttendanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='RG-1', first_name='Re', last_name='Gen',
            department=dept, biometric_id='7701')
        Employee.objects.filter(pk=cls.emp.pk).update(created_at=aware(DAY, time(0)))

    def _log(self, t, log_type, source=AttendanceLog.Source.DEVICE, d=DAY):
        return AttendanceLog.objects.create(employee=self.emp, log_type=log_type,
                                            log_datetime=aware(d, t), source=source)

    def _run(self, *args):
        out = StringIO()
        call_command('regenerate_attendance', *args, stdout=out)
        return out.getvalue()

    def _types(self):
        return [(timezone.localtime(l.log_datetime).time(), l.log_type)
                for l in AttendanceLog.objects.filter(employee=self.emp).order_by('log_datetime')]

    def test_forgotten_am_out_is_relabelled(self):
        # Stored by the old rules: the 12:40 tap became the PM_IN.
        self._log(time(8, 0), 'AM_IN')
        self._log(time(12, 40), 'PM_IN')
        self._log(time(12, 55), 'PM_OUT')
        self._run()
        self.assertEqual(self._types(), [(time(8, 0), 'AM_IN'), (time(12, 40), 'AM_OUT'),
                                         (time(12, 55), 'PM_IN')])

    def test_a_double_tap_is_dropped(self):
        self._log(time(8, 0), 'AM_IN')
        self._log(time(8, 2), 'AM_OUT')        # 2 minutes later: a duplicate now
        self._log(time(13, 0), 'PM_IN')
        self._log(time(17, 0), 'PM_OUT')
        out = self._run()
        self.assertIn('dropped (duplicate)', out)
        self.assertEqual(self._types(), [(time(8, 0), 'AM_IN'), (time(13, 0), 'PM_IN'),
                                         (time(17, 0), 'PM_OUT')])

    def test_manual_entries_are_never_touched(self):
        self._log(time(8, 0), 'AM_IN', source=AttendanceLog.Source.MANUAL)
        self._log(time(8, 2), 'AM_OUT', source=AttendanceLog.Source.MANUAL)
        self._run()
        self.assertEqual(AttendanceLog.objects.filter(employee=self.emp).count(), 2)

    def test_dry_run_changes_nothing(self):
        self._log(time(8, 0), 'AM_IN')
        self._log(time(8, 2), 'AM_OUT')
        out = self._run('--dry-run')
        self.assertIn('Would re-label 0 punch(es), would drop 1 duplicate(s)', out)
        self.assertEqual(AttendanceLog.objects.filter(employee=self.emp).count(), 2)
        self.assertFalse(Absence.objects.exists())

    def test_derived_rows_are_recomputed_and_a_rerun_is_a_no_op(self):
        self._log(time(8, 20), 'AM_IN')
        self._log(time(12, 0), 'AM_OUT')
        self._run()
        self.assertTrue(Lates.objects.filter(employee=self.emp, date=DAY).exists())
        self.assertTrue(Absence.objects.filter(employee=self.emp, date=DAY, session='PM').exists())
        # The days after DAY (through yesterday) were filled in too.
        self.assertTrue(Absence.objects.filter(employee=self.emp,
                                               date=DAY + timedelta(days=1)).exists())

        before = (self._types(), Absence.objects.count(), Lates.objects.count())
        out = self._run()
        self.assertIn('Re-labelled 0 punch(es), dropped 0 duplicate(s)', out)
        self.assertEqual(before, (self._types(), Absence.objects.count(), Lates.objects.count()))
