"""Run the attendance sync service against whatever backend is configured.

    python manage.py sync_attendance                # last 7 days
    python manage.py sync_attendance --days 14
    python manage.py sync_attendance --since 2026-07-01
"""

from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.attendance.services import sync_attendance
from apps.devices.models import BiometricDevice


class Command(BaseCommand):
    help = 'Fetch punches from the configured biometric backend and process them.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=7,
            help='How many days back to sync (default: 7). Ignored if --since given.',
        )
        parser.add_argument(
            '--since', type=str, default=None,
            help='ISO date (YYYY-MM-DD) to sync from. Overrides --days.',
        )

    def handle(self, *args, **options):
        if options['since']:
            try:
                parsed = datetime.strptime(options['since'], '%Y-%m-%d')
            except ValueError:
                raise CommandError('--since must be YYYY-MM-DD')
            since = timezone.make_aware(parsed)
        else:
            since = timezone.now() - timedelta(days=options['days'])

        device = BiometricDevice.objects.filter(is_active=True).first()

        self.stdout.write(f'Syncing attendance since {timezone.localtime(since):%Y-%m-%d %H:%M} ...')
        summary = sync_attendance(device=device, since=since)

        self.stdout.write(self.style.SUCCESS('Sync complete:'))
        for key, value in summary.items():
            self.stdout.write(f'  {key:<16}: {value}')
