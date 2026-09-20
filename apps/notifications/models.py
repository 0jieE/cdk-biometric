from django.db import models

from apps.organization.models import Employee


class Notification(models.Model):
    """A per-employee notification with delivery tracking.

    Per-punch notifications link to their AttendanceLog (unique) so ingestion is
    idempotent — one push per punch, no duplicates on re-sync.
    """

    class Types(models.TextChoices):
        PUNCH = 'PUNCH', 'Punch'
        ATTENDANCE = 'ATTENDANCE', 'Attendance'
        LATE = 'LATE', 'Late'
        ABSENCE = 'ABSENCE', 'Absence'
        GENERAL = 'GENERAL', 'General'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SENT = 'SENT', 'Sent'
        FAILED = 'FAILED', 'Failed'

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='notifications')
    # One notification per attendance log (nullable for non-punch notifications).
    attendance_log = models.OneToOneField(
        'attendance.AttendanceLog', on_delete=models.CASCADE,
        null=True, blank=True, related_name='notification')

    title = models.CharField(max_length=140)
    body = models.TextField(blank=True)
    type = models.CharField(max_length=16, choices=Types.choices, default=Types.GENERAL)
    is_read = models.BooleanField(default=False)

    # Delivery tracking.
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'sent_at']),
        ]

    def __str__(self) -> str:
        return f'{self.employee.employee_no}: {self.title}'
