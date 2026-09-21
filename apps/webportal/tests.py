"""Web portal RBAC + Phase 8 page tests."""

from datetime import date, time

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.attendance.models import AttendanceLog, OTAuthorization
from apps.organization.models import Department, Employee, GlobalSchedule
from apps.reports.models import ReportJob
from apps.webportal.views import _provision_employee_account

User = get_user_model()
PASSWORD = 'portalpass12345'


class PortalAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='W-1', first_name='Web', last_name='Emp',
            department=dept, biometric_id='8001', is_fulltime=True)

        cls.admin = User.objects.create(username='admin1', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

        cls.employee = User.objects.create(username='w-1', role=User.Roles.EMPLOYEE,
                                           employee=cls.emp)
        cls.employee.set_password(PASSWORD)
        cls.employee.save()

    def test_login_page_renders(self):
        self.assertEqual(self.client.get(reverse('webportal:login')).status_code, 200)

    def test_admin_can_access_dashboard(self):
        self.client.login(username='admin1', password=PASSWORD)
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Dashboard')

    def test_employee_denied_admin_pages(self):
        self.client.login(username='w-1', password=PASSWORD)
        resp = self.client.get(reverse('webportal:dashboard'))
        # admin_required redirects non-admins back to login.
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('webportal:login'), resp.url)

    def test_anonymous_redirected(self):
        resp = self.client.get(reverse('webportal:employees'))
        self.assertEqual(resp.status_code, 302)

    def test_employee_cannot_login_to_portal(self):
        resp = self.client.post(reverse('webportal:login'),
                                {'username': 'w-1', 'password': PASSWORD})
        # Rendered login page again with an error, not a redirect to dashboard.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'admin accounts only')

    # -- Phase 8 pages ----------------------------------------------------
    def test_new_admin_pages_gated(self):
        for name in ('schedule', 'overtime', 'manual_attendance'):
            # Employee is denied.
            self.client.login(username='w-1', password=PASSWORD)
            self.assertEqual(self.client.get(reverse(f'webportal:{name}')).status_code, 302)
            self.client.logout()
            # Admin gets in.
            self.client.login(username='admin1', password=PASSWORD)
            self.assertEqual(self.client.get(reverse(f'webportal:{name}')).status_code, 200)
            self.client.logout()

    def test_manual_entry_creates_manual_log_and_recomputes(self):
        self.client.login(username='admin1', password=PASSWORD)
        # Date/time now default to the current moment — only employee + type posted.
        resp = self.client.post(reverse('webportal:manual_attendance'), {
            'employee': self.emp.id, 'log_type': 'AM_IN'})
        self.assertEqual(resp.status_code, 200)
        log = AttendanceLog.objects.get(employee=self.emp, log_type='AM_IN')
        self.assertEqual(log.source, AttendanceLog.Source.MANUAL)
        self.assertEqual(log.created_by, self.admin)

    def test_manual_ot_requires_authorization(self):
        self.client.login(username='admin1', password=PASSWORD)
        # OT punch without an authorization must be rejected.
        resp = self.client.post(reverse('webportal:manual_attendance'), {
            'employee': self.emp.id, 'log_type': 'OT_IN'})
        self.assertContains(resp, 'require an OT authorization')
        self.assertFalse(AttendanceLog.objects.filter(log_type='OT_IN').exists())

    def test_live_logs_public_no_login(self):
        self.assertEqual(self.client.get(reverse('webportal:live')).status_code, 200)
        self.assertEqual(self.client.get(reverse('webportal:live_feed')).status_code, 200)


class ReportsPageEmployeeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='RP-1', first_name='Rep', last_name='Orted',
            department=dept, biometric_id='8101', is_fulltime=True)
        cls.admin = User.objects.create(username='admin2', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    def setUp(self):
        self.client.login(username='admin2', password=PASSWORD)

    def test_page_offers_the_new_report_and_an_employee_picker(self):
        resp = self.client.get(reverse('webportal:reports'))
        self.assertContains(resp, 'Employee Attendance (DTR)')
        self.assertContains(resp, 'RP-1 — Rep Orted')

    @patch('apps.webportal.views.generate_report_task')
    def test_employee_report_requires_an_employee(self, task):
        resp = self.client.post(reverse('webportal:reports'), {
            'report_type': 'EMPLOYEE_ATTENDANCE', 'fmt': 'PDF'})
        self.assertContains(resp, 'Choose an employee for this report.')
        self.assertFalse(ReportJob.objects.exists())
        task.delay.assert_not_called()

    @patch('apps.webportal.views.generate_report_task')
    def test_valid_request_queues_a_job_carrying_the_employee(self, task):
        resp = self.client.post(reverse('webportal:reports'), {
            'report_type': 'EMPLOYEE_ATTENDANCE', 'fmt': 'XLSX',
            'employee': self.emp.id, 'start': '2026-07-01', 'end': '2026-07-31'})
        # Success is an empty 204 that tells the page to refresh its jobs list.
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp['HX-Trigger'], 'refreshJobs')
        job = ReportJob.objects.get()
        self.assertEqual(job.report_type, 'EMPLOYEE_ATTENDANCE')
        self.assertEqual(job.params['employee'], self.emp.id)
        self.assertEqual(job.params['start'], '2026-07-01')
        task.delay.assert_called_once_with(job.id)

    @patch('apps.webportal.views.generate_report_task')
    def test_backwards_date_range_rejected(self, task):
        resp = self.client.post(reverse('webportal:reports'), {
            'report_type': 'EMPLOYEE_ATTENDANCE', 'fmt': 'XLSX', 'employee': self.emp.id,
            'start': '2026-07-31', 'end': '2026-07-01'})
        self.assertContains(resp, 'End date must be on or after the start date.')
        self.assertFalse(ReportJob.objects.exists())

    def test_jobs_list_shows_which_employee_a_job_was_for(self):
        ReportJob.objects.create(report_type='EMPLOYEE_ATTENDANCE', fmt='PDF',
                                 params={'employee': self.emp.id})
        resp = self.client.get(reverse('webportal:report_jobs'))
        self.assertContains(resp, 'RP-1 · Rep Orted')

    def test_direct_export_accepts_the_employee_param(self):
        resp = self.client.get(reverse('webportal:export'), {
            'report_type': 'EMPLOYEE_ATTENDANCE', 'fmt': 'XLSX',
            'employee': self.emp.id, 'start': '2026-07-06', 'end': '2026-07-08'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('employee_attendance_RP-1_', resp['Content-Disposition'])
        self.assertTrue(resp.content.startswith(b'PK'))

    def test_reports_page_is_admin_only(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('webportal:reports')).status_code, 302)


class ProvisionAccountTests(TestCase):
    """The portal creates an employee's login from their employee number. Employees
    can now rename themselves, so that default name may already be taken."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        cls.dept = Department.objects.create(name='IT', code='IT')

    def _employee(self, no, bio):
        return Employee.objects.create(employee_no=no, first_name='New', last_name='Hire',
                                       department=self.dept, biometric_id=bio)

    def test_a_taken_default_username_gets_a_suffix_instead_of_silently_skipping(self):
        # Someone already holds "emp-9001". Previously no account was created at all
        # (and the portal still announced "Login created"), leaving the new hire
        # unable to sign in with no error anywhere.
        User.objects.create(username='emp-9001', role=User.Roles.EMPLOYEE)
        hire = self._employee('EMP-9001', '9001')
        _provision_employee_account(hire)
        account = User.objects.get(employee=hire)
        self.assertEqual(account.username, 'emp-9001-2')
        self.assertTrue(account.check_password('employee12345'))

    def test_is_idempotent_and_keeps_a_login_the_employee_renamed(self):
        hire = self._employee('EMP-9002', '9002')
        _provision_employee_account(hire)
        User.objects.filter(employee=hire).update(username='custom.name')
        _provision_employee_account(hire)              # e.g. an admin clicks "Create login"
        self.assertEqual(User.objects.filter(employee=hire).count(), 1)
        self.assertEqual(User.objects.get(employee=hire).username, 'custom.name')

    def test_normal_case_still_uses_the_employee_number(self):
        hire = self._employee('EMP-9003', '9003')
        _provision_employee_account(hire)
        self.assertEqual(User.objects.get(employee=hire).username, 'emp-9003')



class ScheduleMidpointFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        cls.admin = User.objects.create(username='admin3', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    def _post(self, midpoint):
        self.client.login(username='admin3', password=PASSWORD)
        return self.client.post(reverse('webportal:schedule'), {
            'am_in': '08:00', 'am_out': '12:00', 'pm_in': '13:00', 'pm_out': '17:00',
            'midpoint': midpoint, 'grace_period_minutes': 5, 'workdays': [0, 1, 2, 3, 4]})

    def test_page_shows_the_default_midpoint(self):
        self.client.login(username='admin3', password=PASSWORD)
        self.assertContains(self.client.get(reverse('webportal:schedule')), 'value="12:30"')

    def test_midpoint_can_be_changed(self):
        self._post('12:15')
        self.assertEqual(GlobalSchedule.load().midpoint, time(12, 15))

    def test_midpoint_outside_the_working_day_is_rejected(self):
        resp = self._post('18:00')
        self.assertContains(resp, 'Must fall between')
        self.assertEqual(GlobalSchedule.load().midpoint, time(12, 30))
