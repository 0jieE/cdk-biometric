"""Web portal RBAC + Phase 8 page tests."""

from datetime import date, time

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.attendance.models import AttendanceLog, OTAuthorization
from apps.devices.models import BiometricDevice
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


class ReportsPageTests(TestCase):
    """The Reports page: employee + date range (+ format) -> Attendance report."""

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

    def test_page_offers_only_employee_and_date_range(self):
        resp = self.client.get(reverse('webportal:reports'))
        self.assertEqual(list(resp.context['form'].fields),
                         ['employee', 'start', 'end', 'fmt'])
        self.assertContains(resp, 'RP-1 — Rep Orted')

    @patch('apps.webportal.views.generate_report_task')
    def test_valid_request_queues_an_attendance_job(self, task):
        resp = self.client.post(reverse('webportal:reports'), {
            'employee': self.emp.id, 'fmt': 'PDF',
            'start': '2026-07-01', 'end': '2026-07-31'})
        # Success is an empty 204 that tells the page to refresh its jobs list.
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp['HX-Trigger'], 'refreshJobs')
        job = ReportJob.objects.get()
        self.assertEqual(job.report_type, 'ATTENDANCE')
        self.assertEqual(job.params, {'employee': self.emp.id,
                                      'start': '2026-07-01', 'end': '2026-07-31'})
        task.delay.assert_called_once_with(job.id)

    @patch('apps.webportal.views.generate_report_task')
    def test_employee_and_dates_are_required(self, task):
        resp = self.client.post(reverse('webportal:reports'), {'fmt': 'PDF'})
        self.assertContains(resp, 'This field is required.')
        self.assertFalse(ReportJob.objects.exists())

    @patch('apps.webportal.views.generate_report_task')
    def test_backwards_date_range_rejected(self, task):
        resp = self.client.post(reverse('webportal:reports'), {
            'employee': self.emp.id, 'fmt': 'PDF',
            'start': '2026-07-31', 'end': '2026-07-01'})
        self.assertContains(resp, 'End date must be on or after the start date.')
        self.assertFalse(ReportJob.objects.exists())

    def test_jobs_list_shows_the_employee_and_range(self):
        ReportJob.objects.create(
            report_type='ATTENDANCE', fmt='PDF',
            params={'employee': self.emp.id, 'start': '2026-07-01', 'end': '2026-07-31'})
        resp = self.client.get(reverse('webportal:report_jobs'))
        self.assertContains(resp, 'RP-1 · Rep Orted')
        self.assertContains(resp, '2026-07-01 to 2026-07-31')

    def test_direct_export_builds_the_attendance_report(self):
        resp = self.client.get(reverse('webportal:export'), {
            'report_type': 'ATTENDANCE', 'fmt': 'PDF', 'employee': self.emp.id,
            'start': '2026-07-06', 'end': '2026-07-08'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attendance_RP-1_', resp['Content-Disposition'])
        self.assertTrue(resp.content.startswith(b'%PDF'))

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


class DeviceTestConnectionTests(TestCase):
    """The per-device "Test connection" button must check THAT device's own
    address, not whichever device happens to be marked active."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create(username='admin4', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    def setUp(self):
        self.client.login(username='admin4', password=PASSWORD)

    @patch('apps.webportal.views.get_device_client')
    def test_tests_the_clicked_device_s_own_address(self, get_client):
        active = BiometricDevice.objects.create(
            name='Active', ip_address='192.168.1.3', port=4370, is_active=True)
        other = BiometricDevice.objects.create(
            name='Other', ip_address='192.168.1.9', port=4370, is_active=False)

        self.client.get(reverse('webportal:device_test', args=[other.pk]))

        get_client.assert_called_once_with(ip='192.168.1.9', port=4370)


class EmployeeDeactivationTests(TestCase):
    """Deactivating is one switch (employee + login together), not a separate
    "disable login" that left attendance/headcount untouched."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='DA-1', first_name='De', last_name='Active',
            department=dept, biometric_id='9501', is_fulltime=True)
        cls.admin = User.objects.create(username='admin5', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()
        _provision_employee_account(cls.emp)

    def setUp(self):
        self.client.login(username='admin5', password=PASSWORD)

    def test_deactivate_turns_off_both_employee_and_login(self):
        self.client.post(reverse('webportal:employee_deactivate', args=[self.emp.pk]))
        self.emp.refresh_from_db()
        self.assertFalse(self.emp.is_active)
        self.assertFalse(User.objects.get(employee=self.emp).is_active)

    def test_reactivate_turns_both_back_on(self):
        self.client.post(reverse('webportal:employee_deactivate', args=[self.emp.pk]))
        self.client.post(reverse('webportal:employee_reactivate', args=[self.emp.pk]))
        self.emp.refresh_from_db()
        self.assertTrue(self.emp.is_active)
        self.assertTrue(User.objects.get(employee=self.emp).is_active)

    def test_deactivated_employee_drops_out_of_the_headcount(self):
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertEqual(resp.context['total_employees'], 1)
        self.client.post(reverse('webportal:employee_deactivate', args=[self.emp.pk]))
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertEqual(resp.context['total_employees'], 0)

    def test_the_account_modal_offers_deactivate_then_reactivate(self):
        resp = self.client.get(reverse('webportal:employee_account', args=[self.emp.pk]))
        self.assertContains(resp, 'Deactivate account')
        self.client.post(reverse('webportal:employee_deactivate', args=[self.emp.pk]))
        resp = self.client.get(reverse('webportal:employee_account', args=[self.emp.pk]))
        self.assertContains(resp, 'Reactivate account')
        self.assertNotContains(resp, 'Deactivate account')

    def test_disable_and_enable_are_no_longer_actions_on_the_account_endpoint(self):
        # Old "disable login" only ever touched the account, never the employee
        # or the headcount - that split behaviour is gone; these actions are now
        # no-ops (the view falls through and just re-triggers a refresh).
        self.client.post(reverse('webportal:employee_account', args=[self.emp.pk]),
                         {'action': 'disable'})
        self.assertTrue(User.objects.get(employee=self.emp).is_active)
        self.assertTrue(Employee.objects.get(pk=self.emp.pk).is_active)

    def test_employees_page_separates_active_from_deactivated(self):
        other = Employee.objects.create(
            employee_no='DA-2', first_name='Still', last_name='Here',
            department=self.emp.department, biometric_id='9502', is_fulltime=True)
        self.client.post(reverse('webportal:employee_deactivate', args=[self.emp.pk]))
        # The cards (and their data-status) are loaded via the htmx partial,
        # not the initial page shell.
        resp = self.client.get(reverse('webportal:employees'), headers={'HX-Request': 'true'})
        self.assertContains(resp, 'data-status="inactive"')
        self.assertContains(resp, 'data-status="active"')
        # Both employees are in the payload for the client-side tabs to split -
        # the deactivated one just carries the "inactive" status.
        body = resp.content.decode()
        self.assertIn(self.emp.employee_no, body)
        self.assertIn(other.employee_no, body)
