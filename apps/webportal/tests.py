"""Web portal RBAC + Phase 8 page tests."""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.attendance.models import AttendanceLog, OTAuthorization
from apps.organization.models import Department, Employee, GlobalSchedule

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
