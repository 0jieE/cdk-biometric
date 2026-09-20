"""Real-time attendance listener.

Streams punches from the biometric unit via pyzk's ``live_capture()`` and
ingests each one the instant it happens — firing the immediate per-punch
notification with no polling delay. This is the true real-time path; interval
polling (`sync_attendance` on Celery Beat) remains the fallback.

Run as its own long-lived process/container when hardware is connected:

    BIOMETRIC_DEVICE_BACKEND=zk python manage.py attendance_listener

The mock backend does not stream (it uses polling), so this command reports that
and exits when BIOMETRIC_DEVICE_BACKEND=mock.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.attendance.services import ingest_punches
from apps.devices.clients import get_device_client
from apps.devices.models import BiometricDevice


class Command(BaseCommand):
    help = 'Stream punches from the device in real time (pyzk live_capture).'

    def handle(self, *args, **options):
        backend = getattr(settings, 'BIOMETRIC_DEVICE_BACKEND', 'mock')
        self.stdout.write(f'Device backend: {backend}')

        client = get_device_client()
        device = BiometricDevice.objects.filter(is_active=True).first()

        try:
            stream = client.live_capture()
        except NotImplementedError as exc:
            raise CommandError(
                f'{exc}\nLive capture needs BIOMETRIC_DEVICE_BACKEND=zk with a '
                f'connected unit. The mock backend uses interval polling — run '
                f'`python manage.py sync_attendance` (or let Celery Beat do it).')

        self.stdout.write(self.style.SUCCESS(
            'Listening for punches in real time (Ctrl+C to stop)…'))
        try:
            for punch in stream:
                summary = ingest_punches([punch], device=device)
                self.stdout.write(
                    f'  {punch.timestamp:%Y-%m-%d %H:%M:%S} bio={punch.biometric_id} '
                    f'-> created={summary["created"]} '
                    f'dup={summary["duplicates"]} skipped={summary["skipped"]}')
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('\nListener stopped.'))
