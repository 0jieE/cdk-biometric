"""Manually check this server's clock against an online time source.

    python manage.py sync_time

Normally unnecessary - the periodic attendance sync already does this every
SYNC_INTERVAL_MINUTES - but useful right after deploying, or to confirm
connectivity/offset without waiting for the next cycle.
"""

from django.core.management.base import BaseCommand

from apps.organization.models import TimeSync
from apps.organization.trusted_time import refresh_offset


class Command(BaseCommand):
    help = "Check this server's clock against an online time source and save the gap."

    def handle(self, *args, **options):
        ok = refresh_offset()
        sync = TimeSync.load()
        if ok:
            self.stdout.write(self.style.SUCCESS(
                f'Synced: offset is {sync.offset_seconds:+.2f}s '
                f'(this server is {"ahead" if sync.offset_seconds < 0 else "behind"} '
                f'by {abs(sync.offset_seconds):.1f}s).'))
        else:
            self.stdout.write(self.style.WARNING(
                f'Could not reach the online time source. Keeping the last known '
                f'offset: {sync.offset_seconds:+.2f}s (last synced {sync.last_synced_at or "never"}).'))
