"""Schedule resolver tests."""

from datetime import date, time, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.attendance.models import OTAuthorization
from apps.organization.models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
    Holiday,
    TimeSync,
)
from apps.organization.schedule import get_effective_schedule

SATURDAY = date(2026, 7, 11)  # a Saturday
MONDAY = date(2026, 7, 6)


class ScheduleResolverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        g = GlobalSchedule.load()
        g.am_in, g.am_out, g.pm_in, g.pm_out = time(8, 0), time(12, 0), time(13, 0), time(17, 0)
        g.grace_period_minutes = 5
        g.workdays = [0, 1, 2, 3, 4]
        g.save()
        cls.dept = Department.objects.create(name='IT', code='IT')

    def _emp(self, no, bio):
        return Employee.objects.create(
            employee_no=no, first_name='X', last_name='Y',
            department=self.dept, biometric_id=bio)

    def test_defaults_from_global(self):
        emp = self._emp('R-1', '4001')
        sched = get_effective_schedule(emp)
        self.assertEqual(sched.am_in, time(8, 0))
        self.assertEqual(sched.workdays, [0, 1, 2, 3, 4])

    def test_override_beats_global_but_null_inherits(self):
        emp = self._emp('R-2', '4002')
        EmployeeSchedule.objects.create(employee=emp, am_in=time(7, 0))  # only am_in set
        sched = get_effective_schedule(emp)
        self.assertEqual(sched.am_in, time(7, 0))       # override wins
        self.assertEqual(sched.pm_in, time(13, 0))      # null -> inherits global
        self.assertEqual(sched.grace_period_minutes, 5)

    def test_saturday_worker(self):
        emp = self._emp('R-3', '4003')
        EmployeeSchedule.objects.create(employee=emp, workdays=[0, 1, 2, 3, 4, 5])
        sched = get_effective_schedule(emp)
        self.assertIn(SATURDAY.weekday(), sched.workdays)   # 5 in workdays

    def test_mon_fri_worker_not_on_saturday(self):
        emp = self._emp('R-4', '4004')
        sched = get_effective_schedule(emp)
        self.assertNotIn(SATURDAY.weekday(), sched.workdays)
        self.assertIn(MONDAY.weekday(), sched.workdays)

    def test_midpoint(self):
        emp = self._emp('R-5', '4005')
        sched = get_effective_schedule(emp)
        self.assertEqual(sched.midpoint, time(12, 30))  # default, independent of lunch times

    def test_midpoint_is_a_setting_not_derived_from_lunch(self):
        g = GlobalSchedule.load()
        g.am_out, g.pm_in = time(11, 0), time(11, 30)   # would derive 11:15
        g.save()
        emp = self._emp('R-6', '4006')
        self.assertEqual(get_effective_schedule(emp).midpoint, time(12, 30))
        g.midpoint = time(12, 0)
        g.save()
        self.assertEqual(get_effective_schedule(emp).midpoint, time(12, 0))
        EmployeeSchedule.objects.create(employee=emp, midpoint=time(12, 45))
        self.assertEqual(get_effective_schedule(emp).midpoint, time(12, 45))


User = get_user_model()


class SeedDataTests(TestCase):
    """seed_data runs on every container start, so it must never create anything
    that changes how REAL punches are counted."""

    def _seed(self):
        call_command('seed_data', stdout=StringIO())

    def test_seeds_no_ot_authorizations_or_invented_holidays(self):
        self._seed()
        self.assertEqual(OTAuthorization.objects.count(), 0)
        self.assertFalse(Holiday.objects.filter(name='Foundation Day').exists())
        # The two genuine fixed-date national holidays are still seeded.
        self.assertEqual(Holiday.objects.count(), 2)

    def test_reseeding_survives_an_employee_renaming_their_login(self):
        # Employees can rename their own login from the mobile app, and seed_data
        # runs on EVERY container start. Looked up by username alone it would miss
        # the renamed account, try to create a second user for the same employee and
        # violate the one-to-one link — crashing the entrypoint (set -e), so the app
        # would never boot again.
        self._seed()
        emp = Employee.objects.get(employee_no='EMP-1001')
        User.objects.filter(employee=emp).update(username='maria.s')
        self._seed()                                   # must not raise
        self.assertEqual(User.objects.filter(employee=emp).count(), 1)
        self.assertEqual(User.objects.get(employee=emp).username, 'maria.s')
        self.assertFalse(User.objects.filter(username='emp-1001').exists())

    def test_reseeding_adds_nothing_new(self):
        self._seed()
        counts = (Holiday.objects.count(), OTAuthorization.objects.count(),
                  Employee.objects.count())
        self._seed()
        self.assertEqual(
            (Holiday.objects.count(), OTAuthorization.objects.count(),
             Employee.objects.count()), counts)


class CleanupSeedArtifactsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        dept = Department.objects.create(name='IT', code='IT')
        emp = Employee.objects.create(
            employee_no='C-1', first_name='C', last_name='C',
            department=dept, biometric_id='5001')
        # What the OLD seed left behind...
        Holiday.objects.create(date=date(2026, 8, 15), name='Foundation Day',
                               type=Holiday.Types.SPECIAL)
        OTAuthorization.objects.create(employee=emp, date=date(2026, 9, 14),
                                       ot_start=time(17, 30),
                                       note='Seed example authorization')
        # ...and genuine rows that must survive.
        Holiday.objects.create(date=date(2026, 12, 30), name='Rizal Day',
                               type=Holiday.Types.REGULAR)
        Holiday.objects.create(date=date(2026, 11, 20), name='Foundation Day',
                               type=Holiday.Types.SPECIAL)   # not the 15th => real
        OTAuthorization.objects.create(employee=emp, date=date(2026, 9, 15),
                                       ot_start=time(18, 0), note='Board meeting')

    def _run(self, **kw):
        call_command('cleanup_seed_artifacts', stdout=StringIO(), **kw)

    def test_dry_run_deletes_nothing(self):
        self._run(dry_run=True)
        self.assertEqual(Holiday.objects.count(), 3)
        self.assertEqual(OTAuthorization.objects.count(), 2)

    def test_removes_only_the_seed_fingerprint(self):
        self._run()
        self.assertEqual(
            set(Holiday.objects.values_list('name', 'date')),
            {('Rizal Day', date(2026, 12, 30)), ('Foundation Day', date(2026, 11, 20))})
        self.assertEqual(
            list(OTAuthorization.objects.values_list('note', flat=True)), ['Board meeting'])

    def test_is_idempotent(self):
        self._run()
        self._run()
        self.assertEqual(Holiday.objects.count(), 2)


class TrustedTimeTests(TestCase):
    """The server's own clock can drift; attendance 'now' should be corrected
    against an online source, and keep using the last known-good correction
    when that source is unreachable."""

    def _mock_response(self, when):
        from email.utils import format_datetime
        from unittest.mock import Mock
        resp = Mock()
        resp.headers = {'Date': format_datetime(when, usegmt=True)}
        return resp

    def test_no_sync_yet_means_zero_offset(self):
        from apps.organization.trusted_time import trusted_now
        self.assertFalse(TimeSync.objects.exists())
        delta = (trusted_now() - timezone.now()).total_seconds()
        self.assertLess(abs(delta), 1)

    def test_successful_sync_saves_the_offset(self):
        from unittest.mock import patch
        from apps.organization.trusted_time import refresh_offset, trusted_now

        online_time = timezone.now() + timedelta(seconds=120)   # server is 2 min behind
        with patch('requests.head', return_value=self._mock_response(online_time)):
            ok = refresh_offset()
        self.assertTrue(ok)

        sync = TimeSync.load()
        self.assertTrue(sync.last_sync_ok)
        self.assertIsNotNone(sync.last_synced_at)
        self.assertAlmostEqual(sync.offset_seconds, 120, delta=2)

        delta = (trusted_now() - timezone.now()).total_seconds()
        self.assertAlmostEqual(delta, 120, delta=2)

    def test_unreachable_source_keeps_the_last_known_offset(self):
        from unittest.mock import patch
        from apps.organization.trusted_time import refresh_offset

        online_time = timezone.now() + timedelta(seconds=300)
        with patch('requests.head', return_value=self._mock_response(online_time)):
            refresh_offset()
        saved = TimeSync.load().offset_seconds

        with patch('requests.head', side_effect=ConnectionError('offline')):
            ok = refresh_offset()
        self.assertFalse(ok)

        sync = TimeSync.load()
        self.assertFalse(sync.last_sync_ok)
        self.assertEqual(sync.offset_seconds, saved)   # unchanged - last known good is kept

    def test_never_synced_and_unreachable_stays_at_zero(self):
        from unittest.mock import patch
        from apps.organization.trusted_time import refresh_offset, trusted_now

        with patch('requests.head', side_effect=ConnectionError('offline')):
            ok = refresh_offset()
        self.assertFalse(ok)
        delta = (trusted_now() - timezone.now()).total_seconds()
        self.assertLess(abs(delta), 1)


class SyncTimeCommandTests(TestCase):
    def test_reports_success(self):
        from email.utils import format_datetime
        from unittest.mock import Mock, patch

        resp = Mock()
        resp.headers = {'Date': format_datetime(timezone.now() + timedelta(seconds=5), usegmt=True)}
        out = StringIO()
        with patch('requests.head', return_value=resp):
            call_command('sync_time', stdout=out)
        self.assertIn('Synced', out.getvalue())

    def test_reports_failure_without_crashing(self):
        from unittest.mock import patch

        out = StringIO()
        with patch('requests.head', side_effect=ConnectionError('offline')):
            call_command('sync_time', stdout=out)
        self.assertIn('Could not reach', out.getvalue())
