"""Recompute the derived attendance rows (absences, lates, undertime, overtime)
for past days.

    python manage.py backfill_attendance                       # tracking start -> yesterday
    python manage.py backfill_attendance --since 2026-09-14 --until 2026-09-20
    python manage.py backfill_attendance --dry-run             # just count, change nothing

Absences are created live, day by day, by the daily job — so any day the stack
was down never got them, leaving the portal's Absences page and the reports
understated. This fills those days in. Safe to re-run at any time (e.g. after an
outage): ``process_day`` is idempotent.

It deliberately calls ``process_day`` and NOT ``process_daily_attendance_task``,
so it never sends "Marked Absent" push notifications about old days. Today is
left to the live job, so ``--until`` defaults to yesterday.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.services import process_day
from apps.attendance.tracking import employee_floor, system_start
from apps.organization.models import Employee


def _parse(value, flag):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f'{flag} must be YYYY-MM-DD')


class Command(BaseCommand):
    help = 'Recompute absences/lates/undertime/overtime for past days (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--since', default=None,
            help='First day to process (YYYY-MM-DD). Default: when tracking began '
                 '(ATTENDANCE_START_DATE, else the earliest recorded punch).')
        parser.add_argument(
            '--until', default=None,
            help='Last day to process (YYYY-MM-DD). Default: yesterday.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report how many employee-days would be processed; change nothing.')

    def handle(self, *args, **options):
        since = _parse(options['since'], '--since') if options['since'] else system_start()
        until = (_parse(options['until'], '--until') if options['until']
                 else timezone.localdate() - timedelta(days=1))
        if since > until:
            self.stdout.write(f'Nothing to do: {since} is after {until}.')
            return

        self.stdout.write(f'Backfilling {since} -> {until}'
                          f'{" (dry run)" if options["dry_run"] else ""} ...')

        employee_days = absences = lates = undertimes = overtime = 0
        employees = Employee.objects.filter(is_active=True).select_related('schedule_override')
        for emp in employees:
            # Never before the employee existed in the system / was hired.
            day = max(since, employee_floor(emp))
            while day <= until:
                employee_days += 1
                if not options['dry_run']:
                    result = process_day(emp, day)
                    absences += result['absences']
                    lates += result['lates']
                    undertimes += result['undertimes']
                    overtime += result['overtime']
                day += timedelta(days=1)

        self.stdout.write(self.style.SUCCESS(
            f'{"Would process" if options["dry_run"] else "Processed"} '
            f'{employee_days} employee-day(s).'))
        if not options['dry_run']:
            self.stdout.write(f'  absences: {absences}  lates: {lates}  '
                              f'undertimes: {undertimes}  overtime: {overtime}')
