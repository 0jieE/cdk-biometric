"""Fill a month with realistic DEMO attendance for every employee, so reports
(and the mobile app) can be tested against a full, varied dataset.

    python manage.py seed_demo_attendance                       # this month, through yesterday
    python manage.py seed_demo_attendance --month 2026-09
    python manage.py seed_demo_attendance --month 2026-09 --clear   # remove it again

The FAKE data is opt-in (never runs automatically) and built so it can't be
mistaken for, or damage, real records:

  * Every log is tagged ``source=DEMO`` (shown as "demo" on the dashboard) and
    every overtime authorization it creates has the note "Demo data".
  * It reuses the mock device simulator for realistic patterns — chronically
    late staff, authorized overtime, unauthorized late leavers, half-days,
    whole-day absences, part-time incompletes — and runs the punches through the
    SAME classification/processing code real punches use, so the reports are
    exercised against production rules.
  * It NEVER notifies anyone. Sending real push notifications about fake punches
    to real phones is exactly what must not happen, so notifications are off.
  * It never touches an employee-day that already holds a real punch or an
    admin-created overtime authorization.
  * Idempotent: a re-run replaces its own rows rather than duplicating them.
  * Only completed days are seeded (through yesterday) — no punches in the future.

``--clear`` deletes the demo rows for the month, then re-derives the real
period's absences so nothing fake is left behind.
"""

import calendar
from datetime import date, datetime, time, timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.api.selectors import monthly_summary
from apps.attendance.models import (
    Absence,
    AttendanceLog,
    Lates,
    OTAuthorization,
    Overtime,
    Undertime,
)
from apps.attendance.services import ingest_punches, process_day
from apps.devices.clients.mock_client import MockDeviceClient
from apps.organization.models import Employee

DEMO = AttendanceLog.Source.DEMO
DEMO_OT_NOTE = 'Demo data'


def _bounds(first: date, last: date):
    """[start, end) as aware datetimes covering first..last inclusive."""
    start = timezone.make_aware(datetime.combine(first, time.min))
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min))
    return start, end


class Command(BaseCommand):
    help = 'Seed (or --clear) realistic DEMO attendance for every employee for a month.'

    def add_arguments(self, parser):
        parser.add_argument('--month', default=None,
                            help='YYYY-MM to seed (default: the current month).')
        parser.add_argument('--clear', action='store_true',
                            help='Remove the demo data for the month instead of seeding.')

    def handle(self, *args, **options):
        today = timezone.localdate()
        try:
            year, mon = ((today.year, today.month) if not options['month']
                         else (int(p) for p in options['month'].split('-')))
            first = date(year, mon, 1)
        except (ValueError, TypeError):
            raise CommandError('--month must be YYYY-MM')
        last = date(year, mon, calendar.monthrange(year, mon)[1])

        if options['clear']:
            return self._clear(first, last)
        self._seed(first, last, until=min(last, today - timedelta(days=1)))

    # -- seed ----------------------------------------------------------------
    def _seed(self, first, last, until):
        if first > until:
            self.stdout.write(f'Nothing to seed: {first:%Y-%m} has no completed days yet.')
            return

        # Replace, don't duplicate: drop any previous demo rows for the month.
        removed = self._delete_demo(first, last)

        # Employee-days holding REAL data are off limits.
        start, end = _bounds(first, until)
        skip = {
            (bio, timezone.localtime(dt).date())
            for bio, dt in AttendanceLog.objects.exclude(source=DEMO)
            .filter(log_datetime__gte=start, log_datetime__lt=end)
            .values_list('employee__biometric_id', 'log_datetime')
        }
        skip |= {
            (bio, d) for bio, d in OTAuthorization.objects.exclude(note=DEMO_OT_NOTE)
            .filter(date__gte=first, date__lte=until)
            .values_list('employee__biometric_id', 'date')
        }

        client = MockDeviceClient(ot_note=DEMO_OT_NOTE, until=until, skip=skip)
        punches = client.fetch_attendance(start)
        summary = ingest_punches(punches, device=None, source=DEMO, notify=False)

        # Days with no punches at all are absences — ingestion only visits days
        # that had punches, so derive every remaining employee-day explicitly.
        employees = list(Employee.objects.filter(is_active=True)
                         .select_related('schedule_override').order_by('employee_no'))
        for emp in employees:
            day = first
            while day <= until:
                process_day(emp, day)
                day += timedelta(days=1)

        self.stdout.write(self.style.SUCCESS(
            f'Seeded DEMO attendance for {first:%Y-%m-%d} -> {until:%Y-%m-%d}: '
            f'{summary["created"]} punches for {len(employees)} employees '
            f'(replaced {removed} earlier demo punches; '
            f'{len(skip)} employee-day(s) with real data left untouched).'))
        self._print_table(employees, year=first.year, month=first.month)
        self.stdout.write(
            f'\nThese are FAKE records (source=DEMO). Remove with:\n'
            f'  python manage.py seed_demo_attendance --month {first:%Y-%m} --clear')

    # -- clear ---------------------------------------------------------------
    def _clear(self, first, last):
        removed = self._delete_demo(first, last)

        # Days that no longer have any log: drop their derived rows (an absence
        # for a day nobody was tracked must not be left behind). Days that still
        # hold a real log are recomputed instead.
        recomputed = wiped = 0
        for emp in Employee.objects.filter(is_active=True).select_related('schedule_override'):
            day = first
            while day <= last:
                start, end = _bounds(day, day)
                if AttendanceLog.objects.filter(
                        employee=emp, log_datetime__gte=start, log_datetime__lt=end).exists():
                    process_day(emp, day)
                    recomputed += 1
                else:
                    for model in (Lates, Undertime, Absence, Overtime):
                        model.objects.filter(employee=emp, date=day).delete()
                    wiped += 1
                day += timedelta(days=1)

        self.stdout.write(self.style.SUCCESS(
            f'Cleared {removed} DEMO punch(es) for {first:%Y-%m}.'))
        # Re-derive the real tracked period's absences (start = earliest real punch).
        call_command('backfill_attendance', stdout=self.stdout)

    # -- helpers -------------------------------------------------------------
    def _delete_demo(self, first, last) -> int:
        """Delete this month's demo logs (their lates/undertime cascade) and the
        demo overtime authorizations. Returns how many punches were removed."""
        start, end = _bounds(first, last)
        logs = AttendanceLog.objects.filter(
            source=DEMO, log_datetime__gte=start, log_datetime__lt=end)
        count = logs.count()
        logs.delete()
        OTAuthorization.objects.filter(
            note=DEMO_OT_NOTE, date__gte=first, date__lte=last).delete()
        return count

    def _print_table(self, employees, year, month):
        self.stdout.write('')
        header = (f'{"Employee":<34}{"Type":<10}{"Present":>8}{"Late":>6}{"Half":>6}'
                  f'{"Absent":>8}{"LateMin":>9}{"UnderMin":>10}{"OTMin":>7}')
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        for emp in employees:
            s = monthly_summary(emp, year, month)
            kind = 'full' if emp.is_fulltime else 'part'
            label = f'{emp.employee_no} {emp.full_name}'[:33]
            self.stdout.write(
                f'{label:<34}{kind:<10}{s["present"]:>8}{s["late"]:>6}{s["half_day"]:>6}'
                f'{s["absent"]:>8}{s["late_minutes"]:>9}{s["undertime_minutes"]:>10}'
                f'{s["overtime_minutes"]:>7}')
