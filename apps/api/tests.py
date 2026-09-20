from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceLog
from apps.attendance.services import process_day
from apps.organization.models import Department, Employee, GlobalSchedule

User = get_user_model()

PASSWORD = 'testpass12345'
D = date(2026, 7, 6)  # a Monday


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class ApiAttendanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')

        cls.emp_a = Employee.objects.create(
            employee_no='EMP-A', first_name='Alice', last_name='A',
            department=dept, biometric_id='2001', is_fulltime=True)
        cls.emp_b = Employee.objects.create(
            employee_no='EMP-B', first_name='Bob', last_name='B',
            department=dept, biometric_id='2002', is_fulltime=False)

        cls.user_a = cls._user('alice', cls.emp_a)
        cls.user_b = cls._user('bob', cls.emp_b)

        # Alice (full-time): 4 clean punches => PRESENT.
        for lt, t in [('AM_IN', time(8, 1)), ('AM_OUT', time(12, 0)),
                      ('PM_IN', time(13, 0)), ('PM_OUT', time(17, 0))]:
            AttendanceLog.objects.create(employee=cls.emp_a, log_type=lt,
                                         log_datetime=aware(D, t))
        # Bob (part-time): only IN => incomplete absent.
        AttendanceLog.objects.create(employee=cls.emp_b, log_type='IN',
                                     log_datetime=aware(D, time(8, 30)))
        process_day(cls.emp_a, D)
        process_day(cls.emp_b, D)

    @staticmethod
    def _user(username, emp):
        u = User.objects.create(username=username, role=User.Roles.EMPLOYEE, employee=emp)
        u.set_password(PASSWORD)
        u.save()
        return u

    def _login(self, username):
        client = APIClient()
        resp = client.post('/api/v1/auth/login/',
                           {'username': username, 'password': PASSWORD}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access"]}')
        return client

    def test_login_returns_tokens(self):
        client = APIClient()
        resp = client.post('/api/v1/auth/login/',
                           {'username': 'alice', 'password': PASSWORD}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('access', resp.data)

    def test_me_includes_employment_type(self):
        resp = self._login('alice').get('/api/v1/me/')
        self.assertEqual(resp.data['employee_no'], 'EMP-A')
        self.assertTrue(resp.data['is_fulltime'])
        self.assertEqual(resp.data['employment_type'], 'FULL_TIME')

    def test_fulltime_attendance_shape(self):
        resp = self._login('alice').get(f'/api/v1/attendance/?start={D}&end={D}')
        self.assertEqual(resp.status_code, 200)
        rec = resp.data['results'][0]
        self.assertEqual(rec['employee_type'], 'FULL_TIME')
        self.assertEqual(rec['day_status'], 'PRESENT')
        self.assertIn('am', rec)
        self.assertIn('pm', rec)
        self.assertEqual(rec['overtime_minutes'], 0)

    def test_parttime_attendance_incomplete(self):
        resp = self._login('bob').get(f'/api/v1/attendance/?start={D}&end={D}')
        rec = resp.data['results'][0]
        self.assertEqual(rec['employee_type'], 'PART_TIME')
        self.assertEqual(rec['day_status'], 'ABSENT')
        self.assertTrue(rec['incomplete'])
        self.assertEqual(rec['missing'], 'OUT')

    def test_cross_employee_isolation(self):
        # Bob's request must never surface Alice's data.
        me = self._login('bob').get('/api/v1/me/')
        self.assertEqual(me.data['employee_no'], 'EMP-B')

    def test_unauthenticated_rejected(self):
        self.assertEqual(APIClient().get('/api/v1/attendance/').status_code, 401)

    def test_admin_without_employee_forbidden(self):
        admin = User.objects.create(username='root', role=User.Roles.ADMIN,
                                    is_staff=True, is_superuser=True)
        admin.set_password(PASSWORD)
        admin.save()
        client = APIClient()
        resp = client.post('/api/v1/auth/login/',
                           {'username': 'root', 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access"]}')
        self.assertEqual(client.get('/api/v1/me/').status_code, 403)

    def test_device_register(self):
        resp = self._login('alice').post(
            '/api/v1/devices/register/',
            {'fcm_token': 'tok-123', 'platform': 'android'}, format='json')
        self.assertEqual(resp.status_code, 201)

    def test_health_public(self):
        self.assertEqual(APIClient().get('/api/v1/health/').status_code, 200)
