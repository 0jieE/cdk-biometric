"""Schedule resolver tests."""

from datetime import date, time
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.attendance.models import OTAuthorization
from apps.organization.models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
    Holiday,
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
        self.assertEqual(sched.midpoint, time(12, 30))  # midpoint of 12:00 and 13:00


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
