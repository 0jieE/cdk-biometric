"""Report generator tests: valid non-empty XLSX + PDF for the new shape."""

from datetime import date, datetime, time
from io import BytesIO

from django.test import TestCase
from django.utils import timezone
from openpyxl import load_workbook

from apps.attendance.models import AttendanceLog
from apps.attendance.services import process_day
from apps.organization.models import Department, Employee, GlobalSchedule
from apps.reports.generators import build_report

D = date(2026, 7, 6)


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


def xlsx_rows(content):
    """Rows of the first sheet of an XLSX file, as plain tuples."""
    return list(load_workbook(BytesIO(content)).active.iter_rows(values_only=True))


class ReportGeneratorTests(TestCase):
    """Tardiness + Absence (the list pages' Export buttons)."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='R-1', first_name='Rep', last_name='Ort',
            department=dept, biometric_id='7001', is_fulltime=True)
        # Late AM => a Lates row; missing PM => an Absence row.
        AttendanceLog.objects.create(employee=cls.emp, log_type='AM_IN',
                                     log_datetime=aware(D, time(8, 30)))
        AttendanceLog.objects.create(employee=cls.emp, log_type='AM_OUT',
                                     log_datetime=aware(D, time(12, 0)))
        process_day(cls.emp, D)

    def _params(self):
        return {'start': '2026-07-01', 'end': '2026-07-31'}

    def test_all_reports_xlsx(self):
        for rt in ['TARDINESS', 'ABSENCE']:
            name, content = build_report(rt, 'XLSX', self._params())
            self.assertTrue(name.endswith('.xlsx'))
            self.assertTrue(content.startswith(b'PK'))

    def test_all_reports_pdf(self):
        for rt in ['TARDINESS', 'ABSENCE']:
            name, content = build_report(rt, 'PDF', self._params())
            self.assertTrue(name.endswith('.pdf'))
            self.assertTrue(content.startswith(b'%PDF'))

    def test_removed_report_types_are_rejected(self):
        for rt in ('DAILY_ATTENDANCE', 'MONTHLY_SUMMARY', 'EMPLOYEE_ATTENDANCE'):
            with self.assertRaises(ValueError):
                build_report(rt, 'XLSX', {})


class AttendanceReportTests(TestCase):
    """The Reports page's report: the Attendance page's table for one employee."""

    RANGE = {'start': '2026-07-06', 'end': '2026-07-12'}

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.ft = Employee.objects.create(
            employee_no='A-1', first_name='Full', last_name='Timer',
            department=dept, biometric_id='7301', is_fulltime=True)
        cls.pt = Employee.objects.create(
            employee_no='A-2', first_name='Part', last_name='Timer',
            department=dept, biometric_id='7302', is_fulltime=False)
        # Full-time: AM in 08:30 (25 late), AM out 12:00, no PM => half day.
        AttendanceLog.objects.create(employee=cls.ft, log_type='AM_IN',
                                     log_datetime=aware(D, time(8, 30)))
        AttendanceLog.objects.create(employee=cls.ft, log_type='AM_OUT',
                                     log_datetime=aware(D, time(12, 0)))
        # Part-time: an IN with no OUT => absent, incomplete.
        AttendanceLog.objects.create(employee=cls.pt, log_type='IN',
                                     log_datetime=aware(D, time(8, 0)))
        for emp in (cls.ft, cls.pt):
            process_day(emp, D)

    def _report(self, emp, fmt='XLSX', **params):
        name, content = build_report('ATTENDANCE', fmt,
                                     {'employee': emp.id, **self.RANGE, **params})
        return name, (xlsx_rows(content) if fmt == 'XLSX' else content)

    def test_fulltime_columns_and_row_match_the_attendance_page(self):
        name, rows = self._report(self.ft)
        self.assertTrue(name.startswith('attendance_A-1_'), name)
        self.assertEqual(rows[0], ('Date', 'Employee', 'AM In', 'AM Out', 'PM In', 'PM Out',
                                   'Day Status', 'Late (min)', 'Undertime (min)',
                                   'Lost (min)', 'OT (min)'))
        self.assertEqual(rows[1], ('Jul 06, 2026', 'Full Timer (A-1)',
                                   '08:30', '12:00', '—', '—', 'HALF DAY', 25, 0, 25, 0))
        self.assertEqual(len(rows), 2)

    def test_parttime_columns_and_row(self):
        _, rows = self._report(self.pt)
        self.assertEqual(rows[0], ('Date', 'Employee', 'In', 'Out', 'Day Status', 'Incomplete'))
        self.assertEqual(rows[1], ('Jul 06, 2026', 'Part Timer (A-2)',
                                   '08:00', '—', 'ABSENT', 'OUT missing'))
        self.assertEqual(len(rows), 2)

    def test_only_the_chosen_employee_appears(self):
        _, rows = self._report(self.ft)
        self.assertNotIn('A-2', ' '.join(str(c) for r in rows for c in r))

    def test_newest_day_first(self):
        AttendanceLog.objects.create(employee=self.ft, log_type='AM_IN',
                                     log_datetime=aware(date(2026, 7, 7), time(8, 0)))
        AttendanceLog.objects.create(employee=self.ft, log_type='AM_OUT',
                                     log_datetime=aware(date(2026, 7, 7), time(12, 0)))
        _, rows = self._report(self.ft)
        self.assertEqual([r[0] for r in rows[1:]], ['Jul 07, 2026', 'Jul 06, 2026'])

    def test_inactive_employee_can_still_be_reported(self):
        Employee.objects.filter(pk=self.ft.pk).update(is_active=False)
        _, rows = self._report(self.ft)
        self.assertEqual(rows[1][6], 'HALF DAY')

    def test_pdf_renders_and_empty_range_is_ok(self):
        name, content = self._report(self.ft, fmt='PDF')
        self.assertTrue(name.endswith('.pdf'))
        self.assertTrue(content.startswith(b'%PDF'))
        _, rows = self._report(self.ft, start='2026-01-05', end='2026-01-06')
        self.assertEqual(len(rows), 1)   # header only

    def test_bad_input_rejected(self):
        with self.assertRaises(ValueError):
            self._report(self.ft, start='2026-07-10', end='2026-07-01')
        with self.assertRaises(ValueError):
            self._report(self.ft, start='2025-01-01', end='2026-07-01')
        with self.assertRaises(ValueError):                     # employee is required
            build_report('ATTENDANCE', 'XLSX', dict(self.RANGE))
        with self.assertRaises(ValueError):
            build_report('ATTENDANCE', 'XLSX', {'employee': 999999, **self.RANGE})


class EmployeeFilterOnExistingReportsTests(TestCase):
    """`employee` narrows Tardiness and Absence to one person."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.a = Employee.objects.create(employee_no='F-1', first_name='A', last_name='A',
                                        department=dept, biometric_id='7201', is_fulltime=True)
        cls.b = Employee.objects.create(employee_no='F-2', first_name='B', last_name='B',
                                        department=dept, biometric_id='7202', is_fulltime=True)
        for emp in (cls.a, cls.b):     # both late in the AM, both missing PM
            AttendanceLog.objects.create(employee=emp, log_type='AM_IN',
                                         log_datetime=aware(D, time(8, 30)))
            AttendanceLog.objects.create(employee=emp, log_type='AM_OUT',
                                         log_datetime=aware(D, time(12, 0)))
            process_day(emp, D)

    def _flat(self, report_type, **params):
        base = {'start': '2026-07-01', 'end': '2026-07-31'}
        _, content = build_report(report_type, 'XLSX', {**base, **params})
        return ' '.join(str(c) for r in xlsx_rows(content) for c in r)

    def test_each_report_can_be_limited_to_one_employee(self):
        for rt in ('TARDINESS', 'ABSENCE'):
            everyone = self._flat(rt)
            self.assertIn('F-1', everyone, rt)
            self.assertIn('F-2', everyone, rt)
            only_a = self._flat(rt, employee=self.a.id)
            self.assertIn('F-1', only_a, rt)
            self.assertNotIn('F-2', only_a, rt)
