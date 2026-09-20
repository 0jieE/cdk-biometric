from django.db import models

from apps.organization.models import Employee, TimeStampedModel, avatar_color_for


class BiometricDevice(TimeStampedModel):
    """A physical ZKTeco fingerprint unit that emits attendance punches."""

    name = models.CharField(max_length=120)
    ip_address = models.GenericIPAddressField()
    port = models.PositiveIntegerField(default=4370)
    location = models.CharField(max_length=120, blank=True)
    serial_no = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    # Optional photo of the unit; falls back to an initials wordmark.
    photo = models.ImageField(upload_to='devices/', null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return f'{self.name} ({self.ip_address}:{self.port})'

    @property
    def initials(self) -> str:
        parts = (self.name or '').split()
        if len(parts) >= 2:
            return (parts[0][:1] + parts[1][:1]).upper()
        return (self.name[:2].upper() if self.name else 'DV')

    @property
    def avatar_color(self) -> str:
        return avatar_color_for(self.serial_no or self.name)


class MobileDevice(TimeStampedModel):
    """An employee's phone, registered for FCM push notifications (Phase 2/3)."""

    class Platforms(models.TextChoices):
        ANDROID = 'ANDROID', 'Android'
        IOS = 'IOS', 'iOS'

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='mobile_devices',
    )
    fcm_token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=16, choices=Platforms.choices)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-last_seen_at']

    def __str__(self) -> str:
        return f'{self.employee.employee_no} / {self.platform}'
