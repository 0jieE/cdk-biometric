"""One-time cleanup of rows the OLD ``seed_data`` created on every container start.

The old seed added, each time it ran:
  * a fake "Foundation Day" holiday on the 15th of whatever month it ran in — so
    every month the stack was restarted in gained a made-up day off; and
  * an example overtime authorization (note "Seed example authorization") for
    EMP-1005 on the run day — which silently reclassifies a real evening punch
    that day as overtime.

Both change how REAL punches are counted, so they don't belong in a live
database. ``seed_data`` no longer creates them; this removes the leftovers.
Matches only the seed's exact fingerprint; ``--dry-run`` shows what would go.

    python manage.py cleanup_seed_artifacts --dry-run
    python manage.py cleanup_seed_artifacts

Afterwards run ``python manage.py backfill_attendance`` so the days that were
wrongly treated as holidays / overtime are recomputed.
"""

from django.core.management.base import BaseCommand

from apps.attendance.models import OTAuthorization
from apps.organization.models import Holiday

SEED_OT_NOTE = 'Seed example authorization'


class Command(BaseCommand):
    help = 'Remove the fake holidays / example OT authorizations the old seed_data created.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be removed; delete nothing.')

    def handle(self, *args, **options):
        dry = options['dry_run']

        holidays = [
            h for h in Holiday.objects.filter(
                name='Foundation Day', type=Holiday.Types.SPECIAL, is_recurring=False)
            if h.date.day == 15
        ]
        ot_auths = list(OTAuthorization.objects.filter(note=SEED_OT_NOTE))

        for h in holidays:
            self.stdout.write(f'  holiday   {h.date}  {h.name}')
        for o in ot_auths:
            self.stdout.write(f'  OT auth   {o.date}  {o.employee.employee_no}  from {o.ot_start}')

        if not dry:
            for h in holidays:
                h.delete()
            for o in ot_auths:
                o.delete()

        verb = 'Would remove' if dry else 'Removed'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {len(holidays)} fake holiday(s) and {len(ot_auths)} example OT authorization(s).'))
        if not dry and (holidays or ot_auths):
            self.stdout.write('Now run: python manage.py backfill_attendance')
