from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user with a coarse role for RBAC.

    ADMIN    -> Django admin / web dashboard (Phase 2).
    EMPLOYEE -> mobile API only; linked one-to-one to an Employee record.
    """

    class Roles(models.TextChoices):
        ADMIN = 'ADMIN', 'Admin'
        EMPLOYEE = 'EMPLOYEE', 'Employee'

    role = models.CharField(
        max_length=16,
        choices=Roles.choices,
        default=Roles.EMPLOYEE,
    )
    # Nullable for admins; every EMPLOYEE user should point at an Employee.
    employee = models.OneToOneField(
        'organization.Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='user',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_admin(self) -> bool:
        return self.role == self.Roles.ADMIN

    @property
    def is_employee(self) -> bool:
        return self.role == self.Roles.EMPLOYEE

    def save(self, *args, **kwargs):
        # Enforce: EMPLOYEE users can never reach the admin / web side.
        if self.role == self.Roles.EMPLOYEE:
            self.is_staff = False
            self.is_superuser = False
        super().save(*args, **kwargs)
