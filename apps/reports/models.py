from django.conf import settings
from django.db import models


class ReportJob(models.Model):
    """Tracks an async report generation request and its resulting file."""

    class ReportType(models.TextChoices):
        DAILY_ATTENDANCE = 'DAILY_ATTENDANCE', 'Daily Attendance'
        MONTHLY_SUMMARY = 'MONTHLY_SUMMARY', 'Monthly Summary'
        TARDINESS = 'TARDINESS', 'Tardiness'
        ABSENCE = 'ABSENCE', 'Absence'
        EMPLOYEE_ATTENDANCE = 'EMPLOYEE_ATTENDANCE', 'Employee Attendance (DTR)'

    class Fmt(models.TextChoices):
        PDF = 'PDF', 'PDF'
        XLSX = 'XLSX', 'Excel'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        DONE = 'DONE', 'Done'
        FAILED = 'FAILED', 'Failed'

    report_type = models.CharField(max_length=32, choices=ReportType.choices)
    fmt = models.CharField(max_length=8, choices=Fmt.choices)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    file = models.FileField(upload_to='reports/', null=True, blank=True)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='report_jobs',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.get_report_type_display()} ({self.fmt}) — {self.status}'

    @property
    def is_done(self) -> bool:
        return self.status == self.Status.DONE
