"""Phase 10: per-punch real-time notification tests."""

from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.attendance.models import AttendanceLog
from apps.attendance.services import ingest_punches
from apps.attendance.tasks import process_daily_attendance_task
from apps.devices.clients import RawPunch
from apps.devices.models import MobileDevice
from apps.notifications.models import Notification
from apps.notifications.services import compose_punch_message, notify_for_log
from apps.notifications.tasks import _deliver, retry_pending_notifications_task
from apps.organization.models import Department, Employee, GlobalSchedule

WORKDAY = date(2026, 7, 6)  # Monday


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()  # am 08:00/12:00, pm 13:00/17:00, grace 5
        dept = Department.objects.create(name='IT', code='IT')
        cls.ft = Employee.objects.create(
            employee_no='FT-1', first_name='Full', last_name='Time',
            department=dept, biometric_id='3001', is_fulltime=True)
        cls.pt = Employee.objects.create(
            employee_no='PT-1', first_name='Part', last_name='Time',
            department=dept, biometric_id='3002', is_fulltime=False)


class IngestionHookTests(_Base):
    def test_ingest_creates_one_linked_notification_idempotent(self):
        punch = RawPunch(biometric_id='3001', timestamp=aware(WORKDAY, time(8, 2)), status=0)
        ingest_punches([punch])

        log = AttendanceLog.objects.get(employee=self.ft, log_type='AM_IN')
        self.assertEqual(Notification.objects.filter(attendance_log=log).count(), 1)

        # Re-ingest the same punch: no duplicate log or notification.
        ingest_punches([punch])
        self.assertEqual(AttendanceLog.objects.filter(employee=self.ft).count(), 1)
        self.assertEqual(Notification.objects.filter(attendance_log=log).count(), 1)

    def test_notify_schedules_immediate_send(self):
        log = AttendanceLog.objects.create(
            employee=self.ft, log_type='PM_OUT', log_datetime=aware(WORKDAY, time(17, 4)))
        with self.captureOnCommitCallbacks() as callbacks:
            notification, created = notify_for_log(log)
        self.assertTrue(created)
        # Two callbacks scheduled on commit: the FCM immediate send, and the
        # live-log SSE publish that wakes the /live/ kiosk page.
        self.assertEqual(len(callbacks), 2)

    @patch('apps.attendance.realtime.publish_new_log')
    def test_notify_publishes_live_update(self, mock_publish):
        log = AttendanceLog.objects.create(
            employee=self.ft, log_type='PM_OUT', log_datetime=aware(WORKDAY, time(17, 4)))
        with self.captureOnCommitCallbacks(execute=True):
            notify_for_log(log)
        mock_publish.assert_called_once_with(log)

    def test_manual_log_notified(self):
        log = AttendanceLog.objects.create(
            employee=self.ft, log_type='AM_IN', log_datetime=aware(WORKDAY, time(8, 0)),
            source=AttendanceLog.Source.MANUAL)
        notify_for_log(log)
        self.assertTrue(Notification.objects.filter(attendance_log=log).exists())


class MessageTests(_Base):
    def test_on_time_message(self):
        log = AttendanceLog.objects.create(
            employee=self.ft, log_type='AM_IN', log_datetime=aware(WORKDAY, time(8, 2)))
        _title, body = compose_punch_message(log)
        self.assertIn('AM In recorded', body)
        self.assertNotIn('Late', body)

    def test_late_message_includes_exact_minutes(self):
        # 08:12 with 08:00 + grace 5 => 7 minutes late.
        log = AttendanceLog.objects.create(
            employee=self.ft, log_type='AM_IN', log_datetime=aware(WORKDAY, time(8, 12)))
        _title, body = compose_punch_message(log)
        self.assertIn('Late by 7 min', body)

    def test_parttime_never_late(self):
        log = AttendanceLog.objects.create(
            employee=self.pt, log_type='IN', log_datetime=aware(WORKDAY, time(11, 0)))
        _title, body = compose_punch_message(log)
        self.assertNotIn('Late', body)


class SendTaskTests(_Base):
    def _pending(self):
        return Notification.objects.create(
            employee=self.ft, title='t', body='b', status=Notification.Status.PENDING)

    @patch('apps.notifications.fcm._ensure_initialized', return_value=True)
    @patch('firebase_admin.messaging.send')
    def test_transient_error_retries(self, mock_send, _init):
        mock_send.side_effect = RuntimeError('network down')
        MobileDevice.objects.create(employee=self.ft, fcm_token='tok', platform='ANDROID')
        n = self._pending()
        outcome = _deliver(n)
        self.assertEqual(outcome, 'retry')
        n.refresh_from_db()
        self.assertEqual(n.status, Notification.Status.PENDING)  # not sent -> retry
        self.assertEqual(n.attempts, 1)
        self.assertIn('network', n.last_error)

    @patch('apps.notifications.fcm._ensure_initialized', return_value=True)
    @patch('firebase_admin.messaging.send')
    def test_permanent_unregistered_pruned_not_retried(self, mock_send, _init):
        from firebase_admin import messaging
        mock_send.side_effect = messaging.UnregisteredError('gone')
        dev = MobileDevice.objects.create(employee=self.ft, fcm_token='dead', platform='ANDROID')
        n = self._pending()
        outcome = _deliver(n)
        self.assertEqual(outcome, 'sent')            # terminal, no retry
        dev.refresh_from_db()
        self.assertFalse(dev.is_active)              # token pruned

    @patch('apps.notifications.fcm._ensure_initialized', return_value=True)
    @patch('firebase_admin.messaging.send')
    def test_success_marks_sent(self, mock_send, _init):
        MobileDevice.objects.create(employee=self.ft, fcm_token='ok', platform='ANDROID')
        n = self._pending()
        outcome = _deliver(n)
        self.assertEqual(outcome, 'sent')
        n.refresh_from_db()
        self.assertEqual(n.status, Notification.Status.SENT)
        self.assertIsNotNone(n.sent_at)


class SweeperTests(_Base):
    def _old_pending(self):
        n = Notification.objects.create(
            employee=self.ft, title='t', body='b', status=Notification.Status.PENDING)
        # Backdate so it's older than the sweeper's min-age cutoff.
        Notification.objects.filter(pk=n.pk).update(
            created_at=timezone.now() - timedelta(minutes=5))
        return n

    def test_sweeper_reenqueues_pending(self):
        n = self._old_pending()
        with patch('apps.notifications.tasks.send_fcm_notification_task.delay') as mock_delay:
            result = retry_pending_notifications_task()
        mock_delay.assert_called_once_with(n.id)
        self.assertEqual(result['reenqueued'], 1)

    def test_sent_notification_not_swept(self):
        n = self._old_pending()
        n.status = Notification.Status.SENT
        n.sent_at = timezone.now()
        n.save()
        with patch('apps.notifications.tasks.send_fcm_notification_task.delay') as mock_delay:
            result = retry_pending_notifications_task()
        mock_delay.assert_not_called()
        self.assertEqual(result['reenqueued'], 0)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, FIREBASE_CREDENTIALS='')
class DailyTaskNoDoubleNotifyTests(_Base):
    def test_daily_task_notifies_absence_not_late(self):
        # Late full-time employee (AM 20 min late) with all four punches.
        for lt, t in [('AM_IN', time(8, 25)), ('AM_OUT', time(12, 0)),
                      ('PM_IN', time(13, 0)), ('PM_OUT', time(17, 0))]:
            AttendanceLog.objects.create(employee=self.ft, log_type=lt,
                                         log_datetime=aware(WORKDAY, t))
        # Part-time employee absent (no punches).
        process_daily_attendance_task(WORKDAY.isoformat())

        self.assertTrue(Notification.objects.filter(type='ABSENCE').exists())
        # Lateness is delivered on the immediate per-punch push, never here.
        self.assertFalse(Notification.objects.filter(type='LATE').exists())
