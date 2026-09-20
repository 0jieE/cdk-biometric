from django.conf import settings
from django.db import models

from apps.organization.models import Employee, TimeStampedModel


class Session(models.TextChoices):
    AM = 'AM', 'Morning'
    PM = 'PM', 'Afternoon'
    FULL = 'FULL', 'Full day'


class AttendanceLog(TimeStampedModel):
    """One processed punch, classified by session/type."""

    class LogType(models.TextChoices):
        # Full-time (four punches + optional authorized overtime)
        AM_IN = 'AM_IN', 'AM In'
        AM_OUT = 'AM_OUT', 'AM Out'
        PM_IN = 'PM_IN', 'PM In'
        PM_OUT = 'PM_OUT', 'PM Out'
        OT_IN = 'OT_IN', 'OT In'
        OT_OUT = 'OT_OUT', 'OT Out'
        # Part-time (single in/out)
        IN = 'IN', 'Time In'
        OUT = 'OUT', 'Time Out'

    class Source(models.TextChoices):
        DEVICE = 'DEVICE', 'Biometric Device'
        MANUAL = 'MANUAL', 'Manual Entry'

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='attendance_logs')
    device = models.ForeignKey(
        'devices.BiometricDevice', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='attendance_logs')
    log_datetime = models.DateTimeField()
    log_type = models.CharField(max_length=8, choices=LogType.choices)
    raw_status = models.IntegerField(null=True, blank=True)
    source = models.CharField(
        max_length=16, choices=Source.choices, default=Source.DEVICE)
    # Who created a MANUAL entry (brownout fallback); null for device punches.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='manual_logs')

    class Meta:
        ordering = ['-log_datetime']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'log_datetime', 'log_type'],
                name='uniq_employee_logdt_logtype',
            )
        ]
        indexes = [models.Index(fields=['employee', 'log_datetime'])]

    def __str__(self) -> str:
        return f'{self.employee.employee_no} {self.log_type} @ {self.log_datetime:%Y-%m-%d %H:%M}'


class Lates(TimeStampedModel):
    """Per-session tardiness, measured in exact minutes (full-time only)."""

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='lates')
    date = models.DateField()
    session = models.CharField(max_length=4, choices=Session.choices, default=Session.AM)
    attendance_log = models.ForeignKey(
        AttendanceLog, on_delete=models.CASCADE, related_name='lateness',
        help_text="The session's IN log that triggered this record.")
    minutes_late = models.PositiveIntegerField()

    class Meta:
        ordering = ['-date']
        verbose_name_plural = 'Lates'
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'date', 'session'],
                name='uniq_employee_late_date_session',
            )
        ]

    def __str__(self) -> str:
        return f'{self.employee.employee_no} {self.session} late {self.minutes_late}m on {self.date}'


class Undertime(TimeStampedModel):
    """Per-session undertime — leaving before the session's scheduled OUT.

    Exact minutes, NO grace period (grace is an arrival-only allowance).
    Full-time only; part-time has no fixed schedule so undertime cannot apply.
    Together with Lates this gives lost time = minutes_late + minutes_undertime.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='undertimes')
    date = models.DateField()
    session = models.CharField(max_length=4, choices=Session.choices, default=Session.AM)
    attendance_log = models.ForeignKey(
        AttendanceLog, on_delete=models.CASCADE, related_name='undertime',
        help_text="The session's OUT log that triggered this record.")
    minutes_undertime = models.PositiveIntegerField()

    class Meta:
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'date', 'session'],
                name='uniq_employee_undertime_date_session',
            )
        ]

    def __str__(self) -> str:
        return (f'{self.employee.employee_no} {self.session} undertime '
                f'{self.minutes_undertime}m on {self.date}')


class Absence(TimeStampedModel):
    """Per-session absence. Supports half-day (AM/PM) and part-time incomplete."""

    class Reason(models.TextChoices):
        NO_PUNCH = 'NO_PUNCH', 'No punch recorded'
        MISSING_IN = 'MISSING_IN', 'Missing time in'
        MISSING_OUT = 'MISSING_OUT', 'Missing time out'

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='absences')
    date = models.DateField()
    session = models.CharField(max_length=4, choices=Session.choices, default=Session.FULL)
    reason = models.CharField(
        max_length=16, choices=Reason.choices, default=Reason.NO_PUNCH)
    # Part-time: exactly one of in/out present (the existing punch is retained).
    incomplete = models.BooleanField(default=False)

    class Meta:
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'date', 'session'],
                name='uniq_employee_absence_date_session',
            )
        ]

    def __str__(self) -> str:
        flag = ' (incomplete)' if self.incomplete else ''
        return f'{self.employee.employee_no} absent {self.session} on {self.date}{flag}'


class OTAuthorization(TimeStampedModel):
    """Admin-granted overtime privilege for a specific employee + date.

    OT is ONLY ever computed when a matching authorization exists — there is no
    automatic/threshold overtime.
    """

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='ot_authorizations')
    date = models.DateField()
    ot_start = models.TimeField(help_text='Overtime counts from this time.')
    ot_end_expected = models.TimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ot_authorizations')
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-date']
        verbose_name = 'OT authorization'
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'date'],
                name='uniq_ot_auth_employee_date',
            )
        ]

    def __str__(self) -> str:
        return f'OT auth {self.employee.employee_no} on {self.date} from {self.ot_start}'


class Overtime(TimeStampedModel):
    """Computed overtime record (full-time only, requires an OTAuthorization)."""

    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, related_name='overtimes')
    date = models.DateField()
    authorization = models.ForeignKey(
        OTAuthorization, on_delete=models.CASCADE, related_name='overtime')
    ot_in_log = models.ForeignKey(
        AttendanceLog, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ot_in_for')
    ot_out_log = models.ForeignKey(
        AttendanceLog, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ot_out_for')
    minutes = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'date'],
                name='uniq_overtime_employee_date',
            )
        ]

    def __str__(self) -> str:
        return f'{self.employee.employee_no} OT {self.minutes}m on {self.date}'
