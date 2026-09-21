from datetime import time

from django.db import models


def default_workdays():
    """Mon–Fri (weekday ints 0=Mon … 6=Sun). Callable so migrations are stable."""
    return [0, 1, 2, 3, 4]


# Fixed palette for wordmark fallback avatars — chosen for white-text legibility.
AVATAR_COLORS = (
    '#6D28D9', '#2563EB', '#0D9488', '#DB2777', '#D97706',
    '#059669', '#4F46E5', '#DC2626', '#0891B2', '#7C3AED',
)


def avatar_color_for(seed: str) -> str:
    """Stable colour pick so a given seed always maps to the same swatch."""
    s = seed or 'x'
    return AVATAR_COLORS[sum(ord(c) for c in s) % len(AVATAR_COLORS)]


class TimeStampedModel(models.Model):
    """Abstract base that stamps creation / update times."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Department(TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=16, unique=True)
    is_active = models.BooleanField(default=True)

    # Optional cover banner + square logo. When absent the UI falls back to a
    # brand-tinted banner and an initials wordmark built from ``code``.
    banner = models.ImageField(upload_to='departments/banners/', null=True, blank=True)
    logo = models.ImageField(upload_to='departments/logos/', null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return f'{self.code} - {self.name}'

    @property
    def initials(self) -> str:
        code = (self.code or '').strip()
        if code:
            return code[:3].upper()
        return (self.name[:2].upper() if self.name else '?')

    @property
    def avatar_color(self) -> str:
        return avatar_color_for(self.code or self.name)


class Employee(TimeStampedModel):
    employee_no = models.CharField(max_length=32, unique=True)
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='employees',
    )
    position = models.CharField(max_length=120, blank=True)
    date_hired = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Optional profile photo. When absent, the UI falls back to an initials
    # wordmark tinted by ``avatar_color``.
    photo = models.ImageField(upload_to='employees/', null=True, blank=True)

    # Full-time -> 4 punches/day (AM/PM sessions, tardiness, authorized OT).
    # Part-time -> in/out only, no set time, no tardiness/overtime.
    is_fulltime = models.BooleanField(default=True)

    # Enrollment / UID on the fingerprint device; maps a raw punch to an employee.
    biometric_id = models.CharField(max_length=32, unique=True)

    class Meta:
        ordering = ['employee_no']

    def __str__(self) -> str:
        return f'{self.employee_no} - {self.full_name}'

    @property
    def full_name(self) -> str:
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def employment_type(self) -> str:
        return 'FULL_TIME' if self.is_fulltime else 'PART_TIME'

    @property
    def initials(self) -> str:
        """Up to two letters for the wordmark fallback avatar."""
        first = (self.first_name or '').strip()
        last = (self.last_name or '').strip()
        letters = (first[:1] + last[:1]).upper()
        return letters or (self.employee_no[:2].upper() if self.employee_no else '?')

    @property
    def avatar_color(self) -> str:
        return avatar_color_for(self.employee_no or self.full_name)


class GlobalSchedule(TimeStampedModel):
    """Institution-wide default work schedule. Enforced single row (pk=1)."""

    am_in = models.TimeField(default=time(8, 0))
    am_out = models.TimeField(default=time(12, 0))
    pm_in = models.TimeField(default=time(13, 0))
    pm_out = models.TimeField(default=time(17, 0))
    # Punches at/before this are AM, after it PM (unless past a session's time out).
    midpoint = models.TimeField(default=time(12, 30))
    grace_period_minutes = models.PositiveIntegerField(default=5)
    workdays = models.JSONField(default=default_workdays)

    class Meta:
        verbose_name = 'Global Schedule'
        verbose_name_plural = 'Global Schedule'

    def __str__(self) -> str:
        return 'Institution default schedule'

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # never delete the singleton

    @classmethod
    def load(cls) -> 'GlobalSchedule':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class EmployeeSchedule(TimeStampedModel):
    """Optional per-employee override. Every field nullable — a null field
    means 'inherit the GlobalSchedule value'."""

    employee = models.OneToOneField(
        Employee,
        on_delete=models.CASCADE,
        related_name='schedule_override',
    )
    am_in = models.TimeField(null=True, blank=True)
    am_out = models.TimeField(null=True, blank=True)
    pm_in = models.TimeField(null=True, blank=True)
    pm_out = models.TimeField(null=True, blank=True)
    midpoint = models.TimeField(null=True, blank=True)
    grace_period_minutes = models.PositiveIntegerField(null=True, blank=True)
    workdays = models.JSONField(null=True, blank=True)

    def __str__(self) -> str:
        return f'Schedule override for {self.employee.employee_no}'


class Holiday(TimeStampedModel):
    class Types(models.TextChoices):
        REGULAR = 'REGULAR', 'Regular'
        SPECIAL = 'SPECIAL', 'Special'

    date = models.DateField(unique=True)
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=16, choices=Types.choices, default=Types.REGULAR)
    is_recurring = models.BooleanField(default=False)
    # Hex colour used to tint the day on the calendar.
    color = models.CharField(max_length=7, default='#DC2626')

    class Meta:
        ordering = ['date']

    def __str__(self) -> str:
        return f'{self.date} - {self.name}'
