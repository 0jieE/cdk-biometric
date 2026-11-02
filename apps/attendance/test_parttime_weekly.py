"""Part-timers are judged per week on hours rendered, not per day.

They can come in any day; a day without them is never ABSENT. Hours per day =
OUT - IN (no break deducted), only with both punches - a lone punch makes the
day INCOMPLETE and worth 0 hours. Each Mon-Sun week is checked against their
weekly requirement: the part-time default set on the Schedule page, or their
own hours if set - and those hours must come from at least the minimum number
of different days (default 2), so one long day doesn't meet the week.
"""

import importlib
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.api.selectors import build_daily_attendance, monthly_summary, weekly_hours
from apps.attendance.models import Absence, AttendanceLog
from apps.attendance.services import process_day
from apps.organization.models import Department, Employee, EmployeeSchedule, GlobalSchedule
from apps.organization.schedule import get_effective_schedule
from apps.reports.generators import build_report

User = get_user_model()
PASSWORD = 'ptweeklypass123'

# Week 1: Mon 6 - Sun 12 Jul 2026; week 2: Mon 13 - Sun 19 Jul 2026.
MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 7, d) for d in range(6, 13))
MON2, WED2, SUN2 = date(2026, 7, 13), date(2026, 7, 15), date(2026, 7, 19)


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        g = GlobalSchedule.load()
        g.parttime_weekly_hours = Decimal('10')
        g.save()
        cls.dept = Department.objects.create(name='IT', code='IT')
        cls.pt = Employee.objects.create(
            employee_no='PT-1', first_name='Part', last_name='Timer',
            department=cls.dept, biometric_id='9901', is_fulltime=False)
        cls.ft = Employee.objects.create(
            employee_no='FT-1', first_name='Full', last_name='Timer',
            department=cls.dept, biometric_id='9902', is_fulltime=True)
        Employee.objects.filter(pk__in=[cls.pt.pk, cls.ft.pk]).update(
            created_at=aware(date(2026, 6, 1), time(0)))
        cls.pt.refresh_from_db()
        cls.ft.refresh_from_db()

    def _day(self, d, t_in=None, t_out=None, emp=None):
        emp = emp or self.pt
        if t_in:
            AttendanceLog.objects.create(employee=emp, log_type='IN', log_datetime=aware(d, t_in))
        if t_out:
            AttendanceLog.objects.create(employee=emp, log_type='OUT', log_datetime=aware(d, t_out))


class ScheduleResolutionTests(_Base):
    def test_default_comes_from_the_part_time_setting(self):
        self.assertEqual(get_effective_schedule(self.pt).weekly_hours, Decimal('10'))
        self.assertEqual(get_effective_schedule(self.pt).weekly_minutes, 600)

    def test_own_hours_override_the_default(self):
        EmployeeSchedule.objects.create(employee=self.pt, weekly_hours=Decimal('7.5'))
        emp = Employee.objects.get(pk=self.pt.pk)
        self.assertEqual(get_effective_schedule(emp).weekly_minutes, 450)


class ProcessDayTests(_Base):
    def test_a_day_without_them_is_never_absent(self):
        result = process_day(self.pt, MON)
        self.assertEqual(result['absences'], 0)
        self.assertFalse(Absence.objects.exists())

    def test_one_punch_is_incomplete_not_absent(self):
        self._day(MON, t_in=time(8, 0))
        result = process_day(self.pt, MON)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertFalse(Absence.objects.exists())

    def test_both_punches_is_present(self):
        self._day(MON, time(8, 0), time(12, 0))
        self.assertEqual(process_day(self.pt, MON)['status'], 'PRESENT')

    def test_full_timers_are_unchanged(self):
        process_day(self.ft, MON)
        self.assertEqual(Absence.objects.filter(employee=self.ft).count(), 2)


class DailyRecordTests(_Base):
    def test_hours_are_out_minus_in_with_no_break_deducted(self):
        self._day(MON, time(8, 0), time(17, 0))
        rec = build_daily_attendance(self.pt, MON, MON)[0]
        self.assertEqual((rec['day_status'], rec['worked_minutes']), ('PRESENT', 540))
        self.assertEqual(rec['lost_minutes'], 0)

    def test_only_days_they_came_in_are_listed(self):
        self._day(TUE, time(9, 0), time(11, 30))
        recs = build_daily_attendance(self.pt, MON, FRI)
        self.assertEqual([r['date'] for r in recs], [TUE])
        self.assertEqual(recs[0]['worked_minutes'], 150)

    def test_lone_punch_is_incomplete_and_worth_nothing(self):
        self._day(WED, t_out=time(16, 0))
        rec = build_daily_attendance(self.pt, WED, WED)[0]
        self.assertEqual((rec['day_status'], rec['missing'], rec['worked_minutes']),
                         ('INCOMPLETE', 'IN', 0))

    def test_weekend_days_count_too(self):
        self._day(SAT, time(8, 0), time(12, 0))
        self.assertEqual(build_daily_attendance(self.pt, SAT, SAT)[0]['worked_minutes'], 240)

    def test_monthly_summary(self):
        self._day(MON, time(8, 0), time(12, 0))
        self._day(TUE, t_in=time(8, 0))
        s = monthly_summary(self.pt, 2026, 7)
        self.assertEqual((s['present'], s['incomplete'], s['absent'], s['worked_minutes']),
                         (1, 1, 0, 240))


class WeeklyHoursTests(_Base):
    def test_met_short_and_in_progress(self):
        self._day(MON, time(8, 0), time(13, 0))     # 5h
        self._day(THU, time(13, 0), time(18, 0))    # 5h -> week 1 = 10h = met
        self._day(MON2, time(8, 0), time(12, 0))    # 4h -> week 2 short
        weeks = weekly_hours(self.pt, MON, SUN2, today=date(2026, 7, 25))
        self.assertEqual([(w['week_start'], w['worked_minutes'], w['status']) for w in weeks],
                         [(MON, 600, 'MET'), (MON2, 240, 'SHORT')])
        self.assertEqual(weeks[1]['remaining_minutes'], 360)
        self.assertEqual(weeks[0]['days_worked'], 2)

        # Mid-week 2 it isn't short yet - it's still running.
        weeks = weekly_hours(self.pt, MON2, SUN2, today=WED2)
        self.assertEqual(weeks[0]['status'], 'IN_PROGRESS')

    def test_a_running_week_already_met_reads_met(self):
        self._day(MON2, time(7, 0), time(13, 0))    # 6h
        self._day(WED2, time(7, 0), time(12, 0))    # 5h -> 11h over 2 days
        weeks = weekly_hours(self.pt, MON2, SUN2, today=WED2)
        self.assertEqual(weeks[0]['status'], 'MET')

    def test_whole_weeks_are_counted_even_past_the_range(self):
        self._day(MON, time(8, 0), time(13, 0))
        self._day(SUN, time(8, 0), time(13, 0))
        weeks = weekly_hours(self.pt, WED, THU, today=date(2026, 7, 25))
        self.assertEqual([(w['week_start'], w['worked_minutes']) for w in weeks], [(MON, 600)])

    def test_own_hours_are_the_bar(self):
        EmployeeSchedule.objects.create(employee=self.pt, weekly_hours=Decimal('4'))
        self._day(MON2, time(8, 0), time(10, 0))
        self._day(WED2, time(8, 0), time(10, 0))
        emp = Employee.objects.get(pk=self.pt.pk)
        weeks = weekly_hours(emp, MON2, SUN2, today=date(2026, 7, 25))
        self.assertEqual((weeks[0]['required_minutes'], weeks[0]['status']), (240, 'MET'))

    def test_weeks_before_they_were_added_are_left_out(self):
        # Tracking itself began in June (the system's first punch)...
        AttendanceLog.objects.create(employee=self.ft, log_type='AM_IN',
                                     log_datetime=aware(date(2026, 6, 1), time(8, 0)))
        # ...but this part-timer was only added in week 2.
        Employee.objects.filter(pk=self.pt.pk).update(created_at=aware(MON2, time(9, 0)))
        emp = Employee.objects.get(pk=self.pt.pk)
        weeks = weekly_hours(emp, MON, SUN2, today=date(2026, 7, 25))
        self.assertEqual([w['week_start'] for w in weeks], [MON2])


class MinimumDaysTests(_Base):
    """All the hours in one day don't meet the week."""

    def test_hours_in_a_single_day_are_short_a_day(self):
        self._day(MON, time(7, 0), time(18, 0))     # 11h, but only one day
        w = weekly_hours(self.pt, MON, SUN, today=date(2026, 7, 25))[0]
        self.assertEqual((w['status'], w['days_worked'], w['required_days']), ('SHORT', 1, 2))
        self.assertEqual((w['remaining_minutes'], w['remaining_days']), (0, 1))

    def test_still_running_week_needs_one_more_day(self):
        self._day(MON2, time(7, 0), time(18, 0))
        w = weekly_hours(self.pt, MON2, SUN2, today=MON2)[0]
        self.assertEqual((w['status'], w['remaining_days']), ('IN_PROGRESS', 1))

    def test_a_second_day_meets_the_week(self):
        self._day(MON, time(7, 0), time(18, 0))
        self._day(SAT, time(8, 0), time(9, 0))      # any day counts, even short
        w = weekly_hours(self.pt, MON, SUN, today=date(2026, 7, 25))[0]
        self.assertEqual((w['status'], w['remaining_days']), ('MET', 0))

    def test_an_incomplete_day_does_not_count_as_a_day(self):
        self._day(MON, time(7, 0), time(18, 0))
        self._day(TUE, t_in=time(8, 0))              # lone punch: 0 hours, not a day
        w = weekly_hours(self.pt, MON, SUN, today=date(2026, 7, 25))[0]
        self.assertEqual((w['status'], w['days_worked']), ('SHORT', 1))

    def test_the_minimum_is_a_setting(self):
        g = GlobalSchedule.load()
        g.parttime_min_days = 1
        g.save()
        self._day(MON, time(7, 0), time(18, 0))
        w = weekly_hours(self.pt, MON, SUN, today=date(2026, 7, 25))[0]
        self.assertEqual((w['status'], w['required_days']), ('MET', 1))

        g.parttime_min_days = 3
        g.save()
        self._day(TUE, time(8, 0), time(9, 0))
        w = weekly_hours(self.pt, MON, SUN, today=date(2026, 7, 25))[0]
        self.assertEqual((w['status'], w['remaining_days']), ('SHORT', 1))


class DropOldAbsencesMigrationTests(_Base):
    def test_removes_part_time_absences_only(self):
        Absence.objects.create(employee=self.pt, date=MON, session='FULL')
        Absence.objects.create(employee=self.ft, date=MON, session='AM')
        migration = importlib.import_module(
            'apps.attendance.migrations.0008_drop_parttime_absences')
        migration.drop_parttime_absences(django_apps, None)
        self.assertEqual(list(Absence.objects.values_list('employee_id', flat=True)),
                         [self.ft.id])


class PortalTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        admin = User.objects.create(username='pt-admin', role=User.Roles.ADMIN,
                                    is_staff=True, is_superuser=True)
        admin.set_password(PASSWORD)
        admin.save()

    def setUp(self):
        self.client.login(username='pt-admin', password=PASSWORD)

    def test_schedule_page_has_a_part_time_tab(self):
        html = self.client.get(reverse('webportal:schedule')).content.decode()
        self.assertIn('id="sched-pt"', html)
        self.assertIn('Default weekly hours', html)
        self.assertIn('Minimum days', html)
        self.assertIn('?type=PART_TIME', html)

    def test_save_the_default(self):
        resp = self.client.post(reverse('webportal:schedule_parttime_default'),
                                {'parttime_weekly_hours': '18.5', 'parttime_min_days': '3'})
        self.assertRedirects(resp, reverse('webportal:schedule') + '?tab=parttime')
        g = GlobalSchedule.load()
        self.assertEqual((g.parttime_weekly_hours, g.parttime_min_days), (Decimal('18.5'), 3))

    def test_default_rejects_nonsense(self):
        self.client.post(reverse('webportal:schedule_parttime_default'),
                         {'parttime_weekly_hours': '0', 'parttime_min_days': '2'})
        self.assertEqual(GlobalSchedule.load().parttime_weekly_hours, Decimal('10'))
        for days in ('0', '8'):
            resp = self.client.post(reverse('webportal:schedule_parttime_default'),
                                    {'parttime_weekly_hours': '12', 'parttime_min_days': days})
            self.assertContains(resp, 'Enter between 1 and 7 days.')
        self.assertEqual(GlobalSchedule.load().parttime_min_days, 2)

    def test_lists_are_split_by_type(self):
        ft = self.client.get(reverse('webportal:employee_schedule_list') + '?type=FULL_TIME')
        pt = self.client.get(reverse('webportal:employee_schedule_list') + '?type=PART_TIME')
        self.assertContains(ft, 'FT-1')
        self.assertNotContains(ft, 'PT-1')
        self.assertContains(pt, 'PT-1')
        self.assertNotContains(pt, 'FT-1')
        self.assertContains(pt, '10 h / week')

    def test_set_and_clear_own_hours(self):
        url = reverse('webportal:employee_weekly_hours', args=[self.pt.id])
        self.assertEqual(self.client.post(url, {'weekly_hours': '6'}).status_code, 204)
        self.assertEqual(EmployeeSchedule.objects.get(employee=self.pt).weekly_hours, Decimal('6'))
        self.assertContains(
            self.client.get(reverse('webportal:employee_schedule_list') + '?type=PART_TIME'),
            'Own hours')

        self.client.post(reverse('webportal:employee_weekly_hours_clear', args=[self.pt.id]))
        self.assertIsNone(EmployeeSchedule.objects.get(employee=self.pt).weekly_hours)

    def test_full_timers_have_no_weekly_hours_form(self):
        url = reverse('webportal:employee_weekly_hours', args=[self.ft.id])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_attendance_weekly_table(self):
        self._day(MON, time(8, 0), time(13, 0))     # 5h
        self._day(TUE, time(8, 0), time(13, 0))     # 5h -> 10h over 2 days: met
        self._day(MON2, time(8, 0), time(10, 0))    # 2h on 1 day -> short
        resp = self.client.get(reverse('webportal:attendance_weekly'),
                               {'date_start': '2026-07-06', 'date_end': '2026-07-19'})
        self.assertContains(resp, 'Met')
        self.assertContains(resp, 'Short by 8h &amp; 1 day')
        self.assertContains(resp, '10h')

    def test_hours_met_in_one_day_is_short_a_day(self):
        self._day(MON, time(7, 0), time(18, 0))
        resp = self.client.get(reverse('webportal:attendance_weekly'),
                               {'date_start': '2026-07-06', 'date_end': '2026-07-12'})
        self.assertContains(resp, 'Short by 1 day<')
        self.assertNotContains(resp, 'bi-check-lg')

    def test_part_time_daily_rows_show_hours(self):
        self._day(MON, time(8, 0), time(12, 30))
        resp = self.client.get(reverse('webportal:attendance_data'), {
            'emp_type': 'PART_TIME', 'date_start': '2026-07-06', 'date_end': '2026-07-12'})
        self.assertEqual(resp.json()['data'][0][4], '4h 30m')

    def test_profile_shows_weekly_stats(self):
        resp = self.client.get(reverse('webportal:employee_detail', args=[self.pt.id]))
        self.assertContains(resp, 'Weeks met')
        self.assertContains(resp, 'Hours rendered')


class ReportTests(_Base):
    def test_excel_has_hours_and_a_weekly_sheet(self):
        self._day(MON, time(8, 0), time(12, 30))
        self._day(TUE, t_in=time(8, 0))
        _, content = build_report('ATTENDANCE', 'XLSX', {
            'employee': self.pt.id, 'start': '2026-07-06', 'end': '2026-07-12'})
        wb = load_workbook(BytesIO(content))
        rows = list(wb['Report'].iter_rows(values_only=True))
        self.assertEqual(rows[0], ('Date', 'Employee', 'In', 'Out', 'Hours', 'Day Status',
                                   'Incomplete'))
        self.assertEqual(rows[-1], ('TOTAL', None, None, None, 4.5, '2 days', '1 incomplete'))
        weeks = list(wb['Weekly Hours'].iter_rows(values_only=True))
        self.assertEqual(weeks[1], ('Jul 06 – Jul 12, 2026', 1, 4.5, 10,
                                    'Short by 5.5 h & 1 day'))

    def test_pdf_renders(self):
        self._day(MON, time(8, 0), time(12, 30))
        name, content = build_report('ATTENDANCE', 'PDF', {
            'employee': self.pt.id, 'start': '2026-07-06', 'end': '2026-07-12'})
        self.assertTrue(content.startswith(b'%PDF'))
