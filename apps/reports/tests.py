"""Report generator tests: valid non-empty XLSX + PDF for the new shape."""

from datetime import date, datetime, time

from django.test import TestCase
from django.utils import timezone

from apps.attendance.models import AttendanceLog
from apps.attendance.services import process_day
from apps.organization.models import Department, Employee, GlobalSchedule
from apps.reports.generators import build_report

D = date(2026, 7, 6)


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class ReportGeneratorTests(TestCase):
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
        return {'date': D.isoformat(), 'month': '2026-07',
                'start': '2026-07-01', 'end': '2026-07-31'}

    def test_all_reports_xlsx(self):
        for rt in ['DAILY_ATTENDANCE', 'MONTHLY_SUMMARY', 'TARDINESS', 'ABSENCE']:
            name, content = build_report(rt, 'XLSX', self._params())
            self.assertTrue(name.endswith('.xlsx'))
            self.assertTrue(content.startswith(b'PK'))

    def test_all_reports_pdf(self):
        for rt in ['DAILY_ATTENDANCE', 'MONTHLY_SUMMARY', 'TARDINESS', 'ABSENCE']:
            name, content = build_report(rt, 'PDF', self._params())
            self.assertTrue(name.endswith('.pdf'))
            self.assertTrue(content.startswith(b'%PDF'))
