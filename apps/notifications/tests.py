"""FCM tests: sending no-ops cleanly when FIREBASE_CREDENTIALS is blank."""

from django.test import TestCase, override_settings

from apps.notifications import fcm
from apps.notifications.models import Notification
from apps.notifications.tasks import send_fcm_notification_task
from apps.organization.models import Department, Employee


@override_settings(FIREBASE_CREDENTIALS='')
class FcmDisabledTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp = Employee.objects.create(
            employee_no='N-1', first_name='No', last_name='Fcm',
            department=dept, biometric_id='9101')
        cls.note = Notification.objects.create(
            employee=cls.emp, title='Hi', body='test', type='GENERAL')

    def setUp(self):
        # Reset the module-level init cache so the blank-credentials path runs.
        fcm._initialized = False
        fcm._enabled = False

    def test_send_to_employee_noops(self):
        result = fcm.send_to_employee(self.note)
        self.assertFalse(result['enabled'])
        self.assertEqual(result['sent'], 0)
        # Not marked as sent because delivery was a no-op.
        self.note.refresh_from_db()
        self.assertIsNone(self.note.sent_at)

    def test_task_runs_without_firebase(self):
        # With FCM disabled the send task is a terminal no-op (no retry loop).
        result = send_fcm_notification_task(self.note.id)
        self.assertEqual(result['outcome'], 'disabled')
        self.note.refresh_from_db()
        self.assertEqual(self.note.status, Notification.Status.SENT)
