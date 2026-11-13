"""Web portal RBAC + Phase 8 page tests."""

from datetime import date, datetime, time, timedelta

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import models
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.attendance.models import Absence, AttendanceLog, Lates, OTAuthorization, Undertime
from apps.devices.models import BiometricDevice
from apps.organization.models import Department, Employee, EmployeeSchedule, GlobalSchedule
from apps.reports.models import ReportJob
from apps.webportal.views import _provision_employee_account

User = get_user_model()
PASSWORD = 'portalpass12345'


def frozen_clock(test, hour, minute=0):
    """Pin the trusted clock (what manual punches are stamped with and checked
    against) to today at hour:minute for the rest of ``test``."""
    moment = timezone.make_aware(datetime.combine(timezone.localdate(), time(hour, minute)))
    for target in ('apps.webportal.views.trusted_now',
                   'apps.organization.trusted_time.trusted_now'):
        patcher = patch(target, return_value=moment)
        patcher.start()
        test.addCleanup(patcher.stop)
    return moment


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

    def test_admin_dropdown_links_to_the_schema_diagram_and_django_admin(self):
        self.client.login(username='admin1', password=PASSWORD)
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertContains(resp, 'https://dbdiagram.io/d/CDK-Biometric-69d7b96d80896296845aed6c')
        self.assertContains(resp, reverse('admin:index'))

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

    def _manual(self, log_type):
        return self.client.post(reverse('webportal:manual_attendance'), {
            'employee': self.emp.id, 'log_type': log_type})

    def setUp(self):
        # Mid-morning: AM In, AM Out and PM In are all open, so the duplicate
        # tests below don't depend on when the suite happens to run.
        frozen_clock(self, 10, 0)

    def _backdate_logs(self, minutes):
        AttendanceLog.objects.filter(employee=self.emp).update(
            log_datetime=models.F('log_datetime') - timedelta(minutes=minutes))

    def test_manual_double_click_is_rejected(self):
        self.client.login(username='admin1', password=PASSWORD)
        self._manual('AM_IN')
        resp = self._manual('AM_IN')
        self.assertContains(resp, 'Duplicate: Web Emp already has AM_IN')
        self.assertEqual(AttendanceLog.objects.filter(employee=self.emp).count(), 1)

    def test_manual_out_right_after_its_in_is_rejected(self):
        self.client.login(username='admin1', password=PASSWORD)
        self._manual('AM_IN')
        resp = self._manual('AM_OUT')
        self.assertContains(resp, 'Too soon')
        self.assertFalse(AttendanceLog.objects.filter(log_type='AM_OUT').exists())

    def test_manual_punch_after_five_minutes_is_fine(self):
        self.client.login(username='admin1', password=PASSWORD)
        self._manual('AM_IN')
        self._backdate_logs(5)
        self._manual('AM_OUT')
        self.assertTrue(AttendanceLog.objects.filter(log_type='AM_OUT').exists())

    def test_unrelated_punch_types_are_not_blocked(self):
        # e.g. AM_OUT then PM_IN over lunch is a real sequence.
        frozen_clock(self, 12, 45)
        self.client.login(username='admin1', password=PASSWORD)
        self._manual('AM_OUT')
        self._manual('PM_IN')
        self.assertEqual(AttendanceLog.objects.filter(employee=self.emp).count(), 2)

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
        # Named after the employee and the date range.
        self.assertIn('Rep_Orted_2026-07-06_to_2026-07-08.pdf', resp['Content-Disposition'])
        self.assertTrue(resp.content.startswith(b'%PDF'))

    def test_reports_page_is_admin_only(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('webportal:reports')).status_code, 302)


class ReportJobsFilterTests(TestCase):
    """The Report Jobs table: search (employee / format / status) + a date
    range on when the job was requested."""

    @classmethod
    def setUpTestData(cls):
        dept = Department.objects.create(name='IT', code='IT')
        cls.alice = Employee.objects.create(
            employee_no='RJ-1', first_name='Alice', last_name='Reyes',
            department=dept, biometric_id='8201', is_fulltime=True)
        cls.bob = Employee.objects.create(
            employee_no='RJ-2', first_name='Bob', last_name='Santos',
            department=dept, biometric_id='8202', is_fulltime=True)
        cls.admin = User.objects.create(username='admin6', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

        cls.alice_job = ReportJob.objects.create(
            report_type='ATTENDANCE', fmt='PDF', status='DONE',
            params={'employee': cls.alice.id, 'start': '2026-07-01', 'end': '2026-07-31'})
        cls.bob_job = ReportJob.objects.create(
            report_type='ATTENDANCE', fmt='XLSX', status='PENDING',
            params={'employee': cls.bob.id, 'start': '2026-07-01', 'end': '2026-07-31'})
        old = ReportJob.objects.create(
            report_type='TARDINESS', fmt='PDF', status='DONE', params={})
        old.created_at = timezone.now() - timedelta(days=90)
        old.save(update_fields=['created_at'])
        cls.old_job = old

    def setUp(self):
        self.client.login(username='admin6', password=PASSWORD)

    def _ids(self, **params):
        resp = self.client.get(reverse('webportal:report_jobs'), params)
        return {j.id for j in resp.context['jobs']}, resp

    def test_no_filters_defaults_to_the_last_30_days(self):
        ids, _ = self._ids()
        self.assertEqual(ids, {self.alice_job.id, self.bob_job.id})
        self.assertNotIn(self.old_job.id, ids)

    def test_search_matches_employee_name(self):
        ids, _ = self._ids(q='Reyes')
        self.assertEqual(ids, {self.alice_job.id})

    def test_search_matches_employee_number(self):
        ids, _ = self._ids(q='RJ-2')
        self.assertEqual(ids, {self.bob_job.id})

    def test_search_matches_format(self):
        ids, _ = self._ids(q='XLSX')
        self.assertEqual(ids, {self.bob_job.id})

    def test_search_matches_status(self):
        ids, _ = self._ids(q='PENDING')
        self.assertEqual(ids, {self.bob_job.id})

    def test_search_with_no_match_is_empty_and_says_so(self):
        ids, resp = self._ids(q='nobody-like-this')
        self.assertEqual(ids, set())
        self.assertContains(resp, 'No report jobs match your filters.')

    def test_date_range_reaches_older_jobs(self):
        ids, _ = self._ids(start='2026-01-01', end=timezone.localdate().isoformat())
        self.assertIn(self.old_job.id, ids)

    def test_date_range_excludes_jobs_outside_it(self):
        ids, _ = self._ids(start=(timezone.now() - timedelta(days=5)).date().isoformat(),
                           end=timezone.localdate().isoformat())
        self.assertNotIn(self.old_job.id, ids)
        self.assertIn(self.alice_job.id, ids)

    def test_report_jobs_endpoint_is_admin_only(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('webportal:report_jobs')).status_code, 302)


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

    def test_midpoint_must_be_in_the_lunch_break(self):
        resp = self._post('11:00')                    # before the AM time out
        self.assertContains(resp, 'Must fall between the AM time out (12:00 PM) '
                                  'and the PM time in (1:00 PM).')

    def test_am_time_out_in_the_evening_is_rejected(self):
        # e.g. 23:30 typed for 11:30 AM.
        self.client.login(username='admin3', password=PASSWORD)
        resp = self.client.post(reverse('webportal:schedule'), {
            'am_in': '07:30', 'am_out': '23:30', 'pm_in': '13:00', 'pm_out': '17:00',
            'midpoint': '12:30', 'grace_period_minutes': 5, 'workdays': [0, 1, 2, 3, 4]})
        self.assertContains(resp, 'Must be at or after the AM time out (11:30 PM).')
        self.assertEqual(GlobalSchedule.load().am_out, time(12, 0))

    def test_an_override_is_checked_with_the_times_it_inherits(self):
        dept = Department.objects.create(name='IT', code='IT')
        emp = Employee.objects.create(employee_no='SO-1', first_name='Sched', last_name='Order',
                                      department=dept, biometric_id='9801')
        self.client.login(username='admin3', password=PASSWORD)
        resp = self.client.post(reverse('webportal:employee_schedule', args=[emp.pk]),
                                {'am_out': '14:00'})       # after the global 1:00 PM PM in
        self.assertContains(resp, 'Must be at or after the AM time out (2:00 PM).')
        self.assertFalse(EmployeeSchedule.objects.filter(employee=emp, am_out=time(14, 0)).exists())


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


class DashboardDevicesCardTests(TestCase):
    """The dashboard has its own Devices card listing every device, each with
    its own live connection badge."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create(username='admin8', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    def setUp(self):
        self.client.login(username='admin8', password=PASSWORD)

    def test_no_devices_shows_an_empty_state(self):
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertContains(resp, 'No devices registered yet.')

    def test_every_device_is_listed_with_its_details(self):
        main = BiometricDevice.objects.create(
            name='Main Entrance', ip_address='192.168.1.3', port=4370,
            location='Lobby', is_active=True)
        annex = BiometricDevice.objects.create(
            name='Annex Gate', ip_address='192.168.1.4', port=4371, is_active=True)
        resp = self.client.get(reverse('webportal:dashboard'))
        for d in (main, annex):
            self.assertContains(resp, d.name)
            self.assertContains(resp, f'{d.ip_address}:{d.port}')
            # each active device polls its own status endpoint
            self.assertContains(resp, reverse('webportal:device_status', args=[d.id]))
        self.assertContains(resp, 'Lobby')
        self.assertContains(resp, 'Never synced')

    def test_an_inactive_device_is_shown_but_not_polled(self):
        old = BiometricDevice.objects.create(
            name='Old Unit', ip_address='192.168.1.8', port=4370, is_active=False)
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertContains(resp, 'Old Unit')
        self.assertContains(resp, 'is-inactive')
        self.assertNotContains(resp, reverse('webportal:device_status', args=[old.id]))

    def test_last_sync_is_shown(self):
        BiometricDevice.objects.create(
            name='Main Entrance', ip_address='192.168.1.3', port=4370, is_active=True,
            last_synced_at=timezone.now() - timedelta(minutes=5))
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertContains(resp, 'Synced 5')

    def test_recent_punches_no_longer_carries_the_device_badge(self):
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertNotContains(resp, 'id="deviceStatus"')


class OnScreenClockTests(TestCase):
    """The dashboard and Live Attendance clocks show the system's (trusted)
    time, not the viewing PC's own clock."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create(username='admin_clock', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    def _expected_ms(self, moment):
        local = timezone.localtime(moment).replace(tzinfo=None)
        return int((local - datetime(1970, 1, 1)).total_seconds() * 1000)

    def test_dashboard_clock_starts_from_the_system_time(self):
        self.client.login(username='admin_clock', password=PASSWORD)
        moment = frozen_clock(self, 9, 15)
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertContains(resp, f'SERVER_MS = {self._expected_ms(moment)}')
        self.assertContains(resp, 'getUTCHours()')

    def test_live_clock_starts_from_the_system_time(self):
        moment = frozen_clock(self, 14, 5)
        resp = self.client.get(reverse('webportal:live'))
        self.assertContains(resp, f'SERVER_MS = {self._expected_ms(moment)}')
        self.assertNotContains(resp, 'var n = new Date(), h')

class DeactivatedEmployeeTotalsTests(TestCase):
    """A deactivated employee keeps their records, but stops counting in every
    total: dashboard, lost-time charts, Lates/Absences pages and reports."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.active = Employee.objects.create(
            employee_no='A-1', first_name='Still', last_name='Here',
            department=dept, biometric_id='9101', is_fulltime=True)
        cls.gone = Employee.objects.create(
            employee_no='A-2', first_name='Already', last_name='Gone',
            department=dept, biometric_id='9102', is_fulltime=True, is_active=False)
        today = timezone.localdate()
        for emp, late in ((cls.active, 10), (cls.gone, 30)):
            log = AttendanceLog.objects.create(
                employee=emp, log_type='AM_IN',
                log_datetime=timezone.make_aware(datetime.combine(today, time(8, 0))))
            Lates.objects.create(employee=emp, date=today, session='AM',
                                 attendance_log=log, minutes_late=late)
        Undertime.objects.create(employee=cls.gone, date=today, session='PM',
                                 attendance_log=log, minutes_undertime=20)
        Absence.objects.create(employee=cls.gone, date=today, session='PM',
                               minutes_absent=240)
        cls.admin = User.objects.create(username='admin_deact', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    def setUp(self):
        self.client.login(username='admin_deact', password=PASSWORD)

    def test_dashboard_counts_only_active_employees(self):
        ctx = self.client.get(reverse('webportal:dashboard')).context
        self.assertEqual(ctx['total_employees'], 1)
        self.assertEqual(ctx['present'], 1)
        self.assertEqual(ctx['late'], 1)
        self.assertEqual(ctx['absent'], 0)
        self.assertEqual([t['employee'] for t in ctx['top_lost']], [self.active])
        self.assertEqual(sum(d['total'] for d in ctx['dept_lost']), 10)

    def test_lates_page_totals_skip_deactivated(self):
        ctx = self.client.get(reverse('webportal:lates')).context
        self.assertEqual(ctx['totals'], {'late': 10, 'under': 0, 'absent': 0, 'lost': 10})
        self.assertEqual({r.employee for r in ctx['rows']}, {self.active})

    def test_absences_page_skips_deactivated(self):
        ctx = self.client.get(reverse('webportal:absences')).context
        self.assertEqual(list(ctx['rows']), [])

    def test_reports_skip_deactivated_unless_named(self):
        from apps.reports.generators import _absence, _tardiness
        self.assertEqual([r[1] for r in _tardiness({})['rows']], ['A-1'])
        self.assertEqual(_absence({})['rows'], [])
        # Asking for that one employee by name still gives their history.
        self.assertEqual([r[1] for r in _tardiness({'employee': self.gone.id})['rows']], ['A-2'])

    def test_reactivating_brings_their_records_back(self):
        self.gone.is_active = True
        self.gone.save()
        ctx = self.client.get(reverse('webportal:dashboard')).context
        self.assertEqual((ctx['late'], ctx['absent']), (2, 1))

class DeviceCardStatusTests(TestCase):
    """Each card on the Devices page shows whether THAT device is reachable,
    active or not."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create(username='admin9', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()
        cls.main = BiometricDevice.objects.create(
            name='Main Entrance', ip_address='192.168.1.3', port=4370, is_active=True)
        cls.hs = BiometricDevice.objects.create(
            name='High School', ip_address='192.168.1.202', port=4370, is_active=False)

    def setUp(self):
        self.client.login(username='admin9', password=PASSWORD)

    def test_every_card_polls_its_own_status(self):
        resp = self.client.get(reverse('webportal:devices'), headers={'HX-Request': 'true'})
        for d in (self.main, self.hs):
            self.assertContains(resp, reverse('webportal:device_status', args=[d.pk]))
        # Must swap into its own cell: inheriting the list's hx-target="#list"
        # replaced every card with the bare badge.
        self.assertContains(resp, 'hx-target="this"', count=2)

    @patch('apps.webportal.views.get_device_client')
    def test_reachable_device_shows_connected(self, get_client):
        get_client.return_value.quick_check.return_value = True
        resp = self.client.get(reverse('webportal:device_status', args=[self.main.pk]))
        self.assertContains(resp, 'Connected')
        get_client.assert_called_once_with(ip='192.168.1.3', port=4370)

    @patch('apps.webportal.views.get_device_client')
    def test_an_inactive_device_is_still_checked_at_its_own_address(self, get_client):
        get_client.return_value.quick_check.return_value = False
        resp = self.client.get(reverse('webportal:device_status', args=[self.hs.pk]))
        self.assertContains(resp, 'Offline')
        get_client.assert_called_once_with(ip='192.168.1.202', port=4370)

    @patch('apps.webportal.views.get_device_client')
    def test_a_connection_error_shows_offline_not_a_crash(self, get_client):
        get_client.side_effect = ConnectionError('offline')
        resp = self.client.get(reverse('webportal:device_status', args=[self.main.pk]))
        self.assertContains(resp, 'Offline')

    def test_status_endpoint_is_admin_only(self):
        self.client.logout()
        resp = self.client.get(reverse('webportal:device_status', args=[self.main.pk]))
        self.assertEqual(resp.status_code, 302)


class ToastTests(TestCase):
    """Notifications pop up (#toasts) instead of sitting in the page content."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        cls.dept = Department.objects.create(name='IT', code='IT')
        cls.admin = User.objects.create(username='admin10', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()
        cls.device = BiometricDevice.objects.create(
            name='Main Entrance', ip_address='192.168.1.3', port=4370, is_active=True)

    def setUp(self):
        self.client.login(username='admin10', password=PASSWORD)

    @patch('apps.webportal.views.get_device_client')
    def test_device_test_result_is_a_toast(self, get_client):
        get_client.return_value.test_connection.return_value = False
        resp = self.client.post(reverse('webportal:device_test', args=[self.device.pk]))
        self.assertContains(resp, 'app-toast toast-error')
        self.assertContains(resp, 'Unreachable')

    def test_device_buttons_target_the_toast_stack(self):
        resp = self.client.get(reverse('webportal:devices'), headers={'HX-Request': 'true'})
        self.assertContains(resp, 'hx-target="#toasts" hx-swap="beforeend"', count=2)
        # htmx 1.9: hx-disabled-elt="this" shares the busy indicator's request
        # counter, so the button's spinner would never stop.
        self.assertNotContains(resp, 'hx-disabled-elt="this"')

    def test_htmx_action_messages_arrive_as_a_toast_event(self):
        import json
        resp = self.client.post(reverse('webportal:employee_weekly_hours_clear', args=[
            Employee.objects.create(employee_no='T-1', first_name='Toast', last_name='Test',
                                    department=self.dept, biometric_id='9701',
                                    is_fulltime=False).pk]),
            headers={'HX-Request': 'true'})
        payload = json.loads(resp['HX-Trigger-After-Settle'])
        self.assertEqual(payload['toast']['items'][0]['level'], 'success')
        self.assertIn('default weekly hours', payload['toast']['items'][0]['text'])
        # ...and they're consumed, so they don't pop up a second time on the next page.
        page = self.client.get(reverse('webportal:dashboard'))
        self.assertNotContains(page, 'default weekly hours')

    def test_page_load_messages_render_in_the_toast_stack_not_the_content(self):
        self.client.post(reverse('webportal:schedule_parttime_default'),
                         {'parttime_weekly_hours': '18',
                          'parttime_min_days': '2'})               # a plain POST + redirect
        page = self.client.get(reverse('webportal:schedule'))
        html = page.content.decode()
        stack = html.index('id="toasts"')
        self.assertIn('app-toast toast-success', html[stack:])
        self.assertNotIn('alert-dismissible', html)


class DashboardLinksTests(TestCase):
    """The dashboard's stat cards and Top 5 Lost Time cards are clickable."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='DL-1', first_name='Lost', last_name='Time',
            department=dept, biometric_id='9601', is_fulltime=True)
        cls.inactive_emp = Employee.objects.create(
            employee_no='DL-2', first_name='Not', last_name='Here',
            department=dept, biometric_id='9602', is_fulltime=True, is_active=False)
        cls.admin = User.objects.create(username='admin7', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

        today = timezone.localdate()
        log = AttendanceLog.objects.create(
            employee=cls.emp, log_type='AM_IN',
            log_datetime=timezone.make_aware(datetime.combine(today, time(8, 20))))
        Lates.objects.create(employee=cls.emp, date=today, session='AM',
                             attendance_log=log, minutes_late=15)
        Undertime.objects.create(employee=cls.emp, date=today, session='PM',
                                 attendance_log=log, minutes_undertime=10)

    def setUp(self):
        self.client.login(username='admin7', password=PASSWORD)

    def test_stat_cards_link_to_the_right_pages_for_today(self):
        resp = self.client.get(reverse('webportal:dashboard'))
        today = timezone.localdate().isoformat()
        content = resp.content.decode()
        self.assertIn(reverse('webportal:dashboard_active_employees'), content)
        self.assertIn(f"{reverse('webportal:attendance')}?date_start={today}&date_end={today}", content)
        self.assertIn(f"{reverse('webportal:lates')}?start={today}&end={today}", content)
        self.assertIn(f"{reverse('webportal:absences')}?start={today}&end={today}", content)

    def test_active_employees_card_opens_via_htmx_modal(self):
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertContains(resp, 'hx-get="%s"' % reverse('webportal:dashboard_active_employees'))
        self.assertContains(resp, 'hx-target="#modalContent"')

    def test_active_employees_modal_lists_only_active_employees(self):
        resp = self.client.get(reverse('webportal:dashboard_active_employees'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'DL-1')
        self.assertNotContains(resp, 'DL-2')
        self.assertContains(resp, reverse('webportal:employee_detail', args=[self.emp.id]))

    def test_active_employees_modal_is_admin_only(self):
        self.client.logout()
        resp = self.client.get(reverse('webportal:dashboard_active_employees'))
        self.assertEqual(resp.status_code, 302)

    def test_top_lost_time_card_links_to_the_employee_profile(self):
        resp = self.client.get(reverse('webportal:dashboard'))
        self.assertContains(resp, reverse('webportal:employee_detail', args=[self.emp.id]))


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


class AddEmployeeOnboardingTests(TestCase):
    """Saving a NEW employee moves the modal on to step 2: the schedule override
    for a full-timer, the weekly hours for a part-timer. Edits just close."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        cls.dept = Department.objects.create(name='IT', code='IT')
        cls.admin = User.objects.create(username='admin6', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    def setUp(self):
        self.client.login(username='admin6', password=PASSWORD)

    def _add(self, no, bio, fulltime):
        data = {'employee_no': no, 'first_name': 'New', 'last_name': 'Hire',
                'department': self.dept.pk, 'biometric_id': bio,
                'is_active': 'on'}
        if fulltime:
            data['is_fulltime'] = 'on'
        return self.client.post(reverse('webportal:employee_create'), data)

    def test_form_has_no_position_field(self):
        resp = self.client.get(reverse('webportal:employee_create'))
        self.assertContains(resp, 'name="employee_no"')
        self.assertNotContains(resp, 'name="position"')

    def test_new_fulltimer_goes_to_the_schedule_step(self):
        resp = self._add('OB-1', '9601', fulltime=True)
        emp = Employee.objects.get(employee_no='OB-1')
        self.assertTemplateUsed(resp, 'webportal/partials/employee_schedule_form.html')
        self.assertContains(resp, 'Step 2 of 2')
        self.assertContains(resp, reverse('webportal:employee_schedule', args=[emp.pk]))
        self.assertEqual(resp['HX-Trigger'], 'refreshList')
        self.assertTrue(User.objects.filter(employee=emp).exists())   # login still made

    def test_new_parttimer_goes_to_the_weekly_hours_step(self):
        resp = self._add('OB-2', '9602', fulltime=False)
        emp = Employee.objects.get(employee_no='OB-2')
        self.assertTemplateUsed(resp, 'webportal/partials/employee_weekly_hours_form.html')
        self.assertContains(resp, 'Step 2 of 2')
        self.assertContains(resp, reverse('webportal:employee_weekly_hours', args=[emp.pk]))

    def test_step_two_does_not_create_an_override_until_saved(self):
        self._add('OB-3', '9603', fulltime=False)
        emp = Employee.objects.get(employee_no='OB-3')
        self.assertFalse(EmployeeSchedule.objects.filter(employee=emp).exists())
        resp = self.client.post(reverse('webportal:employee_weekly_hours', args=[emp.pk]),
                                {'weekly_hours': '12', 'onboarding': '1'})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(EmployeeSchedule.objects.get(employee=emp).weekly_hours, 12)

    def test_saving_all_blank_keeps_no_override_row(self):
        self._add('OB-4', '9604', fulltime=False)
        emp = Employee.objects.get(employee_no='OB-4')
        self.client.post(reverse('webportal:employee_weekly_hours', args=[emp.pk]),
                         {'weekly_hours': '', 'onboarding': '1'})
        self.assertFalse(EmployeeSchedule.objects.filter(employee=emp).exists())

    def test_invalid_step_two_stays_on_step_two(self):
        self._add('OB-5', '9605', fulltime=False)
        emp = Employee.objects.get(employee_no='OB-5')
        resp = self.client.post(reverse('webportal:employee_weekly_hours', args=[emp.pk]),
                                {'weekly_hours': '500', 'onboarding': '1'})
        self.assertContains(resp, 'Step 2 of 2')

    def test_editing_an_employee_just_closes_the_modal(self):
        self._add('OB-6', '9606', fulltime=True)
        emp = Employee.objects.get(employee_no='OB-6')
        resp = self.client.post(reverse('webportal:employee_edit', args=[emp.pk]), {
            'employee_no': 'OB-6', 'first_name': 'Renamed', 'last_name': 'Hire',
            'department': self.dept.pk, 'biometric_id': '9606',
            'is_fulltime': 'on', 'is_active': 'on'})
        self.assertEqual(resp.status_code, 204)
        self.assertIn('closeModal', resp['HX-Trigger'])


class EmployeeRecordsTests(TestCase):
    """The profile lists the employee's day-by-day attendance records, not just
    the analytics."""
    MON, TUE = date(2026, 7, 6), date(2026, 7, 7)

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()                       # 08:00-12:00, 13:00-17:00, grace 5
        dept = Department.objects.create(name='IT', code='IT')
        cls.ft = Employee.objects.create(employee_no='R-1', first_name='Full', last_name='Rec',
                                         department=dept, biometric_id='9801', is_fulltime=True)
        cls.pt = Employee.objects.create(employee_no='R-2', first_name='Part', last_name='Rec',
                                         department=dept, biometric_id='9802', is_fulltime=False)
        # Tracking starts at max(first punch ever, created_at): anchor both in June.
        anchor = Employee.objects.create(employee_no='R-X', first_name='An', last_name='Chor',
                                         department=dept, biometric_id='9899', is_fulltime=True)
        cls._punch(anchor, date(2026, 6, 1), 'AM_IN', time(8, 0))
        Employee.objects.filter(pk__in=[cls.ft.pk, cls.pt.pk]).update(
            created_at=timezone.make_aware(datetime(2026, 6, 1)))
        # Monday: full-timer 15 min late in the AM, missed the PM -> 15 + 240 lost.
        cls._punch(cls.ft, cls.MON, 'AM_IN', time(8, 20))
        cls._punch(cls.ft, cls.MON, 'AM_OUT', time(12, 0))
        # Part-timer: Monday 08:00-12:30, Tuesday a lone IN.
        cls._punch(cls.pt, cls.MON, 'IN', time(8, 0))
        cls._punch(cls.pt, cls.MON, 'OUT', time(12, 30))
        cls._punch(cls.pt, cls.TUE, 'IN', time(9, 0))
        # Late minutes come from the processed Lates rows (the daily job).
        from apps.attendance.services import process_day
        for d in (cls.MON, cls.TUE):
            process_day(Employee.objects.get(pk=cls.ft.pk), d)
        cls.admin = User.objects.create(username='admin11', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()

    @staticmethod
    def _punch(emp, d, log_type, t):
        AttendanceLog.objects.create(employee=emp, log_type=log_type,
                                     log_datetime=timezone.make_aware(datetime.combine(d, t)))

    def setUp(self):
        self.client.login(username='admin11', password=PASSWORD)

    def _records(self, emp, start, end, **extra):
        return self.client.get(reverse('webportal:employee_records', args=[emp.pk]),
                               {'start': start.isoformat(), 'end': end.isoformat(), **extra})

    def test_profile_loads_the_records_section(self):
        resp = self.client.get(reverse('webportal:employee_detail', args=[self.ft.pk]))
        self.assertContains(resp, reverse('webportal:employee_records', args=[self.ft.pk]))

    def test_fulltime_rows_and_totals(self):
        resp = self._records(self.ft, self.MON, self.TUE)
        self.assertEqual([r['day_status'] for r in resp.context['records']],
                         ['ABSENT', 'HALF_DAY'])                    # newest first
        t = resp.context['totals']
        self.assertEqual((t['late_minutes'], t['absent_minutes'], t['lost_minutes']),
                         (15, 240 + 480, 15 + 240 + 480))
        self.assertContains(resp, '08:20')
        self.assertContains(resp, 'Half day')

    def test_status_filter_keeps_the_counts_of_the_whole_range(self):
        resp = self._records(self.ft, self.MON, self.TUE, status='ABSENT')
        self.assertEqual([r['date'] for r in resp.context['records']], [self.TUE])
        self.assertEqual(resp.context['status_counts'],
                         [('HALF_DAY', 'Half day', 1), ('ABSENT', 'Absent', 1)])

    def test_export_links_cover_the_selected_range(self):
        resp = self._records(self.ft, self.MON, self.TUE)
        self.assertContains(resp, f'employee={self.ft.id}&start=2026-07-06&end=2026-07-07')

    def test_reversed_range_is_swapped(self):
        resp = self._records(self.ft, self.TUE, self.MON)
        self.assertEqual((resp.context['start'], resp.context['end']), (self.MON, self.TUE))

    def test_parttime_shows_hours_and_its_weeks(self):
        resp = self._records(self.pt, self.MON, self.TUE)
        self.assertEqual([r['day_status'] for r in resp.context['records']],
                         ['INCOMPLETE', 'PRESENT'])
        self.assertEqual(resp.context['totals']['worked'], '4h 30m')
        self.assertEqual(len(resp.context['weeks']), 1)
        self.assertContains(resp, 'OUT missing')
        self.assertNotContains(resp, '<th>Employee</th>')       # it's their own profile

    def test_records_are_admin_only(self):
        self.client.logout()
        self.assertEqual(self._records(self.ft, self.MON, self.MON).status_code, 302)


class HowItWorksTests(TestCase):
    """The help page explains the rules using the live schedule settings."""

    @classmethod
    def setUpTestData(cls):
        g = GlobalSchedule.load()
        g.am_in, g.grace_period_minutes = time(7, 30), 10
        g.parttime_weekly_hours, g.parttime_min_days = 12, 3
        g.save()
        cls.admin = User.objects.create(username='help-admin', role=User.Roles.ADMIN,
                                        is_staff=True, is_superuser=True)
        cls.admin.set_password(PASSWORD)
        cls.admin.save()
        cls.employee = User.objects.create(username='help-emp', role=User.Roles.EMPLOYEE)
        cls.employee.set_password(PASSWORD)
        cls.employee.save()

    def test_sidebar_links_to_it_with_the_exclamation_icon(self):
        self.client.login(username='help-admin', password=PASSWORD)
        html = self.client.get(reverse('webportal:dashboard')).content.decode()
        link = html[html.index(reverse('webportal:how_it_works')) - 200:]
        self.assertIn('bi-exclamation-circle', link[:500])

    def test_page_uses_the_current_settings(self):
        self.client.login(username='help-admin', password=PASSWORD)
        resp = self.client.get(reverse('webportal:how_it_works'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'nav-link active', count=1)
        for section in ('recording', 'sorting', 'manual', 'statuses', 'lost-time',
                        'overtime', 'part-time', 'holidays', 'schedules', 'notifications'):
            self.assertContains(resp, f'id="{section}"')
        # Grace ends 7:40; the late example scans in 7 minutes past it.
        self.assertContains(resp, 'grace until 7:40 AM')
        self.assertContains(resp, 'scanned in at 7:47 AM')
        self.assertContains(resp, '<b>12 hours</b>', html=False)
        self.assertContains(resp, '<b>3 different days</b>', html=False)
        self.assertIn('short by 2 days', ' '.join(resp.content.decode().split()))
        # Manual entry windows, from the same settings (AM In 7:30, midpoint 12:30, PM In 1:00).
        text = ' '.join(resp.content.decode().split())
        self.assertIn('When each punch can be recorded', text)
        self.assertIn('AM Out</span></td> <td>7:30 AM', text)
        self.assertIn('PM In</span></td> <td>12:30 PM', text)
        self.assertIn('PM Out</span></td> <td>1:00 PM', text)

    def test_admins_only(self):
        self.client.login(username='help-emp', password=PASSWORD)
        resp = self.client.get(reverse('webportal:how_it_works'))
        self.assertEqual(resp.status_code, 302)


class ManualSessionWindowTests(TestCase):
    """Manual entry: a session's In closes once its time out has passed, and
    its Out only opens at its time in (default 8-12 / 1-5)."""

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.ft = Employee.objects.create(
            employee_no='MW-1', first_name='Full', last_name='Timer',
            department=dept, biometric_id='9701', is_fulltime=True)
        cls.pt = Employee.objects.create(
            employee_no='MW-2', first_name='Part', last_name='Timer',
            department=dept, biometric_id='9702', is_fulltime=False)
        admin = User.objects.create(username='mw-admin', role=User.Roles.ADMIN,
                                    is_staff=True, is_superuser=True)
        admin.set_password(PASSWORD)
        admin.save()

    def setUp(self):
        self.client.login(username='mw-admin', password=PASSWORD)

    def _punch(self, log_type, emp=None):
        return self.client.post(reverse('webportal:manual_attendance'), {
            'employee': (emp or self.ft).id, 'log_type': log_type})

    def _recorded(self, log_type):
        return AttendanceLog.objects.filter(log_type=log_type).exists()

    def test_am_in_is_closed_after_the_morning_ends(self):
        frozen_clock(self, 12, 40)
        resp = self._punch('AM_IN')
        self.assertContains(resp, 'AM_IN is closed: the morning session ended at 12:00 PM.')
        self.assertFalse(self._recorded('AM_IN'))
        self._punch('PM_IN')                       # the afternoon is still open
        self.assertTrue(self._recorded('PM_IN'))

    def test_am_in_at_exactly_time_out_is_closed(self):
        frozen_clock(self, 12, 0)
        self._punch('AM_IN')
        self.assertFalse(self._recorded('AM_IN'))

    def test_pm_in_is_closed_after_the_afternoon_ends(self):
        frozen_clock(self, 17, 5)
        resp = self._punch('PM_IN')
        self.assertContains(resp, 'PM_IN is closed: the afternoon session ended at 5:00 PM.')
        self.assertFalse(self._recorded('PM_IN'))

    def test_outs_are_not_open_before_their_session_starts(self):
        frozen_clock(self, 7, 30)
        resp = self._punch('AM_OUT')
        self.assertContains(resp, 'AM_OUT is not open yet: the morning session starts at 8:00 AM.')
        resp = self._punch('PM_OUT')
        self.assertContains(resp, 'PM_OUT is not open yet: the afternoon session starts at 1:00 PM.')
        self.assertFalse(AttendanceLog.objects.exists())
        self._punch('AM_IN')                       # early arrival is fine
        self.assertTrue(self._recorded('AM_IN'))

    def test_am_out_closes_when_the_afternoon_starts(self):
        frozen_clock(self, 12, 59)
        self._punch('AM_OUT')                      # still lunch: fine
        self.assertTrue(self._recorded('AM_OUT'))
        AttendanceLog.objects.all().delete()
        frozen_clock(self, 13, 0)
        resp = self._punch('AM_OUT')
        self.assertContains(resp, 'AM_OUT is closed: the afternoon session started at 1:00 PM.')
        self.assertFalse(self._recorded('AM_OUT'))

    def test_pm_in_opens_at_the_midpoint(self):
        frozen_clock(self, 9, 0)
        resp = self._punch('PM_IN')
        self.assertContains(resp, 'PM_IN is not open yet: it opens at the midpoint, 12:30 PM.')
        frozen_clock(self, 12, 29)
        self._punch('PM_IN')
        self.assertFalse(self._recorded('PM_IN'))
        frozen_clock(self, 12, 30)
        self._punch('PM_IN')
        self.assertTrue(self._recorded('PM_IN'))

    def test_outs_open_once_their_session_starts(self):
        frozen_clock(self, 13, 0)
        self._punch('PM_OUT')
        self.assertTrue(self._recorded('PM_OUT'))

    def test_the_employees_own_schedule_is_used(self):
        EmployeeSchedule.objects.create(employee=self.ft, am_out=time(11, 0))
        frozen_clock(self, 11, 30)
        resp = self._punch('AM_IN')
        self.assertContains(resp, 'the morning session ended at 11:00 AM.')

    def test_am_in_never_outlasts_the_midpoint(self):
        # A bad saved schedule (AM time out 11:30 PM) can't keep AM In open.
        GlobalSchedule.objects.filter(pk=1).update(am_out=time(23, 30))
        frozen_clock(self, 15, 52)
        resp = self._punch('AM_IN')
        self.assertContains(resp, 'AM_IN is closed: the morning session ended at 12:30 PM.')
        self.assertFalse(self._recorded('AM_IN'))

    def test_part_timers_have_no_window(self):
        frozen_clock(self, 21, 0)
        self._punch('IN', emp=self.pt)
        self.assertTrue(self._recorded('IN'))

    def test_terminal_gets_schedules_and_the_server_clock(self):
        frozen_clock(self, 9, 15)
        html = self.client.get(reverse('webportal:manual_attendance')).content.decode()
        self.assertIn('"sched": {"am_in": 480, "am_out": 720, "pm_in": 780, "pm_out": 1020, '
                      '"midpoint": 750}', html)
        moment = datetime.combine(timezone.localdate(), time(9, 15))
        self.assertIn(f'SERVER_MS = {int((moment - datetime(1970, 1, 1)).total_seconds() * 1000)}', html)
