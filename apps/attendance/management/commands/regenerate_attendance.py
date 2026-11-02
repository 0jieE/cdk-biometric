"""Bring stored attendance in line with the CURRENT rules, after the rules or
the schema change.

    python manage.py regenerate_attendance --dry-run   # show what would change
    python manage.py regenerate_attendance             # apply

Two passes:

1. Re-classify stored punches. Every employee-day with device (or demo)
   punches is run through the same classifier live punches use, so each stored
   punch carries the type the rules give it today - e.g. a 12:40 punch stored
   as PM_IN that is really a forgotten AM_OUT. Punches the rules now treat as
   duplicates (a second tap within 5 minutes) are deleted, along with that
   punch's own notification. Manual entries are never changed or deleted.
2. Re-derive absences, lates, undertime and overtime for every day since
   tracking began (``backfill_attendance``), plus any re-classified day that
   is today. Part-time weekly results are computed on the fly, so they follow
   automatically.

Never sends notifications. Safe to re-run: a second run finds nothing to change.
"""

from collections import defaultdict

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.attendance.models import AttendanceLog
from apps.attendance.services import classify_day, process_day
from apps.organization.schedule import get_effective_schedule
from apps.organization.trusted_time import trusted_localdate

MANUAL = AttendanceLog.Source.MANUAL


class Command(BaseCommand):
    help = 'Re-classify stored punches and re-derive attendance by the current rules.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change; change nothing.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        self.stdout.write(f'Re-classifying stored punches{" (dry run)" if dry else ""} ...')
        relabelled, dropped, days = self._reclassify(dry)
        self.stdout.write(self.style.SUCCESS(
            f'{"Would re-label" if dry else "Re-labelled"} {relabelled} punch(es), '
            f'{"would drop" if dry else "dropped"} {dropped} duplicate(s) '
            f'on {len(days)} employee-day(s).'))

        if dry:
            call_command('backfill_attendance', dry_run=True, stdout=self.stdout)
            return

        today = trusted_localdate()
        for emp, day in sorted(days, key=lambda x: (x[0].pk, x[1])):
            if day >= today:          # the backfill below stops at yesterday
                process_day(emp, day)
        call_command('backfill_attendance', stdout=self.stdout)

    def _reclassify(self, dry):
        logs = (AttendanceLog.objects.select_related('employee__schedule_override')
                .filter(employee__is_active=True).order_by('log_datetime', 'pk'))
        per_day = defaultdict(list)
        for log in logs:
            per_day[(log.employee, timezone.localtime(log.log_datetime).date())].append(log)

        relabelled = dropped = 0
        changed_days = set()
        scheds = {}
        for (emp, day), day_logs in per_day.items():
            if all(log.source == MANUAL for log in day_logs):
                continue
            sched = scheds.setdefault(emp.pk, get_effective_schedule(emp))
            times = sorted({log.log_datetime for log in day_logs})
            wanted = {dt: t for t, dt in classify_day(emp, sched, day, times).items()}

            # Per instant: keep one non-manual row, typed as the rules say.
            relabel, drop = [], []
            by_instant = defaultdict(list)
            for log in day_logs:
                if log.source != MANUAL:
                    by_instant[log.log_datetime].append(log)
            for dt, rows in by_instant.items():
                log_type = wanted.get(dt)
                manual_has_it = any(l.source == MANUAL and l.log_datetime == dt
                                    and l.log_type == log_type for l in day_logs)
                if log_type is None or manual_has_it:
                    drop.extend(rows)          # a duplicate, or already a manual entry
                    continue
                keep = next((r for r in rows if r.log_type == log_type), rows[0])
                drop.extend(r for r in rows if r is not keep)
                if keep.log_type != log_type:
                    relabel.append((keep, log_type))

            if not (relabel or drop):
                continue
            changed_days.add((emp, day))
            for log, log_type in relabel:
                self.stdout.write(f'  {self._label(log)} -> {log_type}')
            for log in drop:
                self.stdout.write(f'  {self._label(log)} dropped (duplicate)')
            relabelled += len(relabel)
            dropped += len(drop)
            if not dry:
                with transaction.atomic():
                    AttendanceLog.objects.filter(pk__in=[l.pk for l in drop]).delete()
                    for log, log_type in relabel:
                        log.log_type = log_type
                        log.save(update_fields=['log_type'])
        return relabelled, dropped, changed_days

    @staticmethod
    def _label(log):
        when = timezone.localtime(log.log_datetime)
        return f'{log.employee.employee_no} {when:%Y-%m-%d %H:%M:%S} {log.log_type} ({log.source.lower()})'
