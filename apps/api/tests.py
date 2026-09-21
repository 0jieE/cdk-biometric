from datetime import date, datetime, time

import time as time_module

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
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

    def _client_with_token_issued(self, seconds_from_now):
        """A client whose access token claims it was issued `seconds_from_now`
        seconds from the server's clock (positive = in the 'future')."""
        now = int(time_module.time())
        token = jwt.encode(
            {'token_type': 'access', 'exp': now + 3600, 'iat': now + seconds_from_now,
             'jti': 'skewtest', 'user_id': self.user_a.id},
            settings.SECRET_KEY, algorithm='HS256')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def test_token_tolerates_small_clock_skew(self):
        # Docker Desktop's VM clock can step back ~1s, making a just-issued token
        # look "issued in the future". With no leeway that was a random logout.
        for skew in (1, 30):
            resp = self._client_with_token_issued(skew).get('/api/v1/me/')
            self.assertEqual(resp.status_code, 200, f'{skew}s skew rejected')

    def test_token_far_in_the_future_still_rejected(self):
        resp = self._client_with_token_issued(600).get('/api/v1/me/')
        self.assertEqual(resp.status_code, 401)

    @override_settings(ATTENDANCE_START_DATE='2026-08-01')
    def test_real_punches_shown_even_before_tracking_start(self):
        # The start date only limits *inferred* days; recorded punches always show.
        resp = self._login('alice').get(f'/api/v1/attendance/?start={D}&end={D}')
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual(resp.data['results'][0]['day_status'], 'PRESENT')


@override_settings(ATTENDANCE_START_DATE='2026-07-01')
class NoPunchInferenceTests(TestCase):
    """A workday with no punches must read ABSENT — never PRESENT just because
    no Absence row exists (the daily job never ran for it) — and days before
    tracking began must not be reported at all."""

    NO_PUNCH_DAY = date(2026, 7, 7)   # Tuesday, after the start date, no punches
    BEFORE_START = date(2026, 6, 30)  # Tuesday, before the start date

    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.full = Employee.objects.create(
            employee_no='EMP-F', first_name='Fay', last_name='F',
            department=dept, biometric_id='3001', is_fulltime=True)
        cls.part = Employee.objects.create(
            employee_no='EMP-P', first_name='Pat', last_name='P',
            department=dept, biometric_id='3002', is_fulltime=False)
        # created_at is auto_now_add; backdate so they existed before the range.
        Employee.objects.update(created_at=aware(date(2026, 6, 1), time(9, 0)))
        for name, emp in (('fay', cls.full), ('pat', cls.part)):
            u = User.objects.create(username=name, role=User.Roles.EMPLOYEE, employee=emp)
            u.set_password(PASSWORD)
            u.save()

    def _get(self, username, start, end):
        client = APIClient()
        token = client.post('/api/v1/auth/login/',
                            {'username': username, 'password': PASSWORD},
                            format='json').data['access']
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client.get(f'/api/v1/attendance/?start={start}&end={end}')

    def test_fulltime_workday_without_punches_is_absent(self):
        rec = self._get('fay', self.NO_PUNCH_DAY, self.NO_PUNCH_DAY).data['results'][0]
        self.assertEqual(rec['day_status'], 'ABSENT')
        self.assertEqual(rec['am']['status'], 'ABSENT')
        self.assertEqual(rec['pm']['status'], 'ABSENT')
        self.assertIsNone(rec['am']['in'])

    def test_parttime_workday_without_punches_is_absent_not_incomplete(self):
        rec = self._get('pat', self.NO_PUNCH_DAY, self.NO_PUNCH_DAY).data['results'][0]
        self.assertEqual(rec['day_status'], 'ABSENT')
        self.assertFalse(rec['incomplete'])
        self.assertIsNone(rec['missing'])

    def test_days_before_tracking_start_are_not_reported(self):
        for who in ('fay', 'pat'):
            resp = self._get(who, self.BEFORE_START, self.BEFORE_START)
            self.assertEqual(resp.data['count'], 0, who)

    def test_weekend_is_not_inferred_absent(self):
        saturday = date(2026, 7, 11)
        self.assertEqual(self._get('fay', saturday, saturday).data['count'], 0)

    def test_summary_counts_absent_days_and_exposes_minute_fields(self):
        client = APIClient()
        token = client.post('/api/v1/auth/login/',
                            {'username': 'fay', 'password': PASSWORD},
                            format='json').data['access']
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        data = client.get('/api/v1/attendance/summary/?month=2026-07').data
        # July 2026 has 23 weekdays, all after the start date, none with punches.
        self.assertEqual(data['absent'], 23)
        self.assertEqual(data['present'], 0)
        for key in ('late_minutes', 'undertime_minutes', 'lost_minutes'):
            self.assertIn(key, data)

    def test_month_before_tracking_start_is_empty(self):
        client = APIClient()
        token = client.post('/api/v1/auth/login/',
                            {'username': 'fay', 'password': PASSWORD},
                            format='json').data['access']
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        data = client.get('/api/v1/attendance/summary/?month=2026-06').data
        self.assertEqual((data['present'], data['late'], data['absent']), (0, 0, 0))
