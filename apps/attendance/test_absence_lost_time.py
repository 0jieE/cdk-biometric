"""Absences count toward lost time: a missed session loses its whole scheduled
length (default AM 08:00-12:00 = 240 min, PM 13:00-17:00 = 240 min, so a whole
missed day = 480), on top of minutes late and minutes undertime."""

import importlib
from datetime import date, datetime, time

from django.apps import apps as django_apps
from django.test import TestCase
from django.utils import timezone

from apps.api.selectors import build_daily_attendance, monthly_summary
from apps.attendance.models import Absence, AttendanceLog
from apps.attendance.services import process_day
from apps.organization.models import Department, Employee, EmployeeSchedule, GlobalSchedule
from apps.organization.schedule import EffectiveSchedule, get_effective_schedule
from apps.webportal.views import (
    _lost_time_by_department,
    _lost_time_by_employee,
    _top_lost_employees,
)

MON = date(2026, 7, 6)
TUE = date(2026, 7, 7)


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class SessionMinutesTests(TestCase):
    sched = EffectiveSchedule(am_in=time(8, 0), am_out=time(12, 0),
                              pm_in=time(13, 0), pm_out=time(17, 30),
                              grace_period_minutes=5, workdays=[0, 1, 2, 3, 4])

    def test_each_session_is_its_own_scheduled_span(self):
        self.assertEqual(self.sched.session_minutes('AM'), 240)
        self.assertEqual(self.sched.session_minutes('PM'), 270)

    def test_a_full_day_is_both_sessions(self):
        self.assertEqual(self.sched.session_minutes('FULL'), 510)


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()   # defaults: 08:00-12:00, 13:00-17:00
        cls.dept = Department.objects.create(name='IT', code='IT')
        cls.ft = Employee.objects.create(
            employee_no='FT-1', first_name='Full', last_name='Time',
            department=cls.dept, biometric_id='4001', is_fulltime=True)
        cls.pt = Employee.objects.create(
            employee_no='PT-1', first_name='Part', last_name='Time',
            department=cls.dept, biometric_id='4002', is_fulltime=False)
        # Tracking starts at max(first punch in the system, created_at): an
        # unrelated June punch plus backdated created_at put July in range.
        anchor = Employee.objects.create(
            employee_no='X-1', first_name='Anchor', last_name='Punch',
            department=cls.dept, biometric_id='4999', is_fulltime=True)
        AttendanceLog.objects.create(employee=anchor, log_type='AM_IN',
                                     log_datetime=aware(date(2026, 6, 1), time(8, 0)))
        Employee.objects.filter(pk__in=[cls.ft.pk, cls.pt.pk]).update(
            created_at=aware(date(2026, 6, 1), time(0, 0)))
        cls.ft.refresh_from_db()
        cls.pt.refresh_from_db()

    def _punch(self, emp, d, log_type, t):
        AttendanceLog.objects.create(employee=emp, log_type=log_type,
                                     log_datetime=aware(d, t))


class ProcessDayAbsentMinutesTests(_Base):
    def test_whole_day_absent_is_eight_hours(self):
        process_day(self.ft, MON)
        self.assertEqual(
            dict(Absence.objects.filter(employee=self.ft).values_list('session', 'minutes_absent')),
            {'AM': 240, 'PM': 240})

    def test_half_day_loses_only_the_missed_session(self):
        self._punch(self.ft, MON, 'AM_IN', time(8, 0))
        self._punch(self.ft, MON, 'AM_OUT', time(12, 0))
        process_day(self.ft, MON)
        ab = Absence.objects.get(employee=self.ft)
        self.assertEqual((ab.session, ab.minutes_absent), ('PM', 240))

    def test_a_missing_punch_absence_still_loses_the_session(self):
        self._punch(self.ft, MON, 'AM_IN', time(8, 0))   # no AM_OUT
        self._punch(self.ft, MON, 'PM_IN', time(13, 0))
        self._punch(self.ft, MON, 'PM_OUT', time(17, 0))
        process_day(self.ft, MON)
        self.assertEqual(Absence.objects.get(employee=self.ft, session='AM').minutes_absent, 240)

    def test_part_timer_is_never_marked_absent(self):
        # Part-timers are judged per week on hours, not per day.
        process_day(self.pt, MON)
        self.assertFalse(Absence.objects.filter(employee=self.pt).exists())

    def test_uses_the_employee_s_own_schedule_override(self):
        EmployeeSchedule.objects.create(employee=self.ft, am_in=time(7, 0))  # AM 07:00-12:00
        emp = Employee.objects.get(pk=self.ft.pk)
        process_day(emp, MON)
        self.assertEqual(Absence.objects.get(employee=emp, session='AM').minutes_absent, 300)


class SelectorLostTimeTests(_Base):
    def test_day_record_adds_absent_minutes_to_lost(self):
        self._punch(self.ft, MON, 'AM_IN', time(8, 20))   # 15 late (grace 5)
        self._punch(self.ft, MON, 'AM_OUT', time(12, 0))
        process_day(self.ft, MON)
        rec = build_daily_attendance(self.ft, MON, MON)[0]
        self.assertEqual(rec['late_minutes'], 15)
        self.assertEqual(rec['absent_minutes'], 240)
        self.assertEqual(rec['lost_minutes'], 255)
        self.assertEqual(rec['pm']['minutes_absent'], 240)
        self.assertEqual(rec['am']['minutes_absent'], 0)

    def test_inferred_absence_counts_even_before_the_daily_job_runs(self):
        # No Absence rows yet - the selector judges from punches, so lost time
        # must too.
        rec = build_daily_attendance(self.ft, MON, MON)[0]
        self.assertEqual(rec['day_status'], 'ABSENT')
        self.assertEqual(rec['lost_minutes'], 480)

    def test_part_timer_day_off_is_not_a_record(self):
        self.assertEqual(build_daily_attendance(self.pt, MON, MON), [])

    def test_monthly_summary_includes_absent_minutes(self):
        for d in (MON, TUE):
            self._punch(self.ft, d, 'AM_IN', time(8, 0))
            self._punch(self.ft, d, 'AM_OUT', time(12, 0))
            self._punch(self.ft, d, 'PM_IN', time(13, 0))
            self._punch(self.ft, d, 'PM_OUT', time(17, 0))
        Employee.objects.filter(pk=self.ft.pk).update(created_at=aware(MON, time(0, 0)))
        emp = Employee.objects.get(pk=self.ft.pk)
        s = monthly_summary(emp, 2026, 7)
        # Every Mon-Fri from Wed 8th to Fri 31st (18 days) is absent.
        self.assertEqual(s['absent'], 18)
        self.assertEqual(s['absent_minutes'], 18 * 480)
        self.assertEqual(s['lost_minutes'], 18 * 480)


class PortalLostTimeTests(_Base):
    def setUp(self):
        self._punch(self.ft, MON, 'AM_IN', time(8, 20))   # 15 late
        self._punch(self.ft, MON, 'AM_OUT', time(12, 0))  # PM missed -> 240
        process_day(self.ft, MON)
        process_day(self.pt, MON)                          # part-timer: nothing

    def test_top_lost_employees_ranks_by_late_under_and_absent(self):
        rows = _top_lost_employees(MON, MON)
        self.assertEqual([(r['employee'].employee_no, r['absent'], r['total']) for r in rows],
                         [('FT-1', 240, 255)])

    def test_department_total_includes_absences(self):
        rows, peak = _lost_time_by_department(MON, MON)
        it = next(r for r in rows if r['code'] == 'IT')
        self.assertEqual((it['late'], it['absent'], it['total']), (15, 240, 255))
        self.assertEqual(peak, 255)

    def test_per_employee_table(self):
        from apps.attendance.models import Lates, Undertime
        rows = _lost_time_by_employee(
            Lates.objects.all(), Undertime.objects.all(), Absence.objects.all())
        ft = next(r for r in rows if r['no'] == 'FT-1')
        self.assertEqual((ft['absent_count'], ft['absent_minutes'], ft['lost_minutes']),
                         (1, 240, 255))


class BackfillMigrationTests(_Base):
    def test_existing_absences_get_their_session_length(self):
        migration = importlib.import_module(
            'apps.attendance.migrations.0006_absence_minutes_absent')
        EmployeeSchedule.objects.create(employee=self.ft, pm_out=time(18, 0))
        Absence.objects.create(employee=self.ft, date=MON, session='AM')
        Absence.objects.create(employee=self.ft, date=MON, session='PM')
        Absence.objects.create(employee=self.pt, date=MON, session='FULL')

        migration.backfill_minutes_absent(django_apps, None)

        got = {(a.employee_id, a.session): a.minutes_absent for a in Absence.objects.all()}
        self.assertEqual(got, {(self.ft.id, 'AM'): 240, (self.ft.id, 'PM'): 300,
                               (self.pt.id, 'FULL'): 480})
        # Same answer the live code would give.
        emp = Employee.objects.get(pk=self.ft.pk)
        self.assertEqual(get_effective_schedule(emp).session_minutes('PM'), 300)
