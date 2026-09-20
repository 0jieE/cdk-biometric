"""Schedule resolver tests."""

from datetime import date, time

from django.test import TestCase

from apps.organization.models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
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
