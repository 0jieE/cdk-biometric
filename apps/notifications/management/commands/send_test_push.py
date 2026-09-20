"""Create a test Notification for an employee and push it via FCM.

Lets us test the end-to-end push path without waiting for the daily attendance
task. The employee must have registered at least one device (a MobileDevice row,
created when the mobile app POSTs its FCM token to /api/v1/devices/register/).

    python manage.py send_test_push EMP-1001
    python manage.py send_test_push EMP-1001 --title "Hello" --body "Custom message"
"""

from django.core.management.base import BaseCommand, CommandError

from apps.devices.models import MobileDevice
from apps.notifications.fcm import send_to_employee
from apps.notifications.models import Notification
from apps.organization.models import Employee


class Command(BaseCommand):
    help = 'Create a Notification for an employee and send it via FCM (test push).'

    def add_arguments(self, parser):
        parser.add_argument('employee_no', help='Employee number, e.g. EMP-1001')
        parser.add_argument('--title', default='Test Notification')
        parser.add_argument(
            '--body',
            default='This is a test push from the Attendance System.',
        )

    def handle(self, *args, **options):
        employee_no = options['employee_no']
        try:
            employee = Employee.objects.get(employee_no=employee_no)
        except Employee.DoesNotExist:
            raise CommandError(f'No employee with employee_no={employee_no!r}.')

        active_tokens = MobileDevice.objects.filter(
            employee=employee, is_active=True).exclude(fcm_token='').count()
        self.stdout.write(
            f'Employee {employee.employee_no} ({employee.full_name}) '
            f'has {active_tokens} active device token(s).'
        )

        notification = Notification.objects.create(
            employee=employee,
            type=Notification.Types.GENERAL,
            title=options['title'],
            body=options['body'],
        )

        result = send_to_employee(notification)

        if not result['enabled']:
            self.stdout.write(self.style.WARNING(
                'FCM is DISABLED (FIREBASE_CREDENTIALS blank or init failed). '
                'Notification was created but not delivered.'))
        elif active_tokens == 0:
            self.stdout.write(self.style.WARNING(
                'FCM is enabled but the employee has no registered device. '
                'Open the mobile app and log in as this employee first, then retry.'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Push sent: delivered={result['sent']}, pruned={result['failed']}."))

        self.stdout.write(f'Result: {result}  (notification id={notification.id})')
