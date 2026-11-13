"""An employee and their login account are deleted together.

Deleting either one (e.g. in Django admin) removes both, and with the
employee every record of theirs goes too (punches, lates, undertime,
absences, overtime, notifications, devices) via on_delete=CASCADE. To keep a
person's history, *deactivate* them on their profile instead of deleting.
"""

from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from apps.accounts.models import User
from apps.organization.models import Employee


@receiver(post_delete, sender=User)
def delete_employee_with_login(sender, instance, **kwargs):
    if instance.employee_id:
        Employee.objects.filter(pk=instance.employee_id).delete()


@receiver(pre_delete, sender=Employee)
def remember_employee_login(sender, instance, **kwargs):
    # Collected now, deleted after: the FK is SET_NULL, so by post_delete the
    # link is gone (which also stops the handler above from recursing).
    instance._login_ids = list(User.objects.filter(employee=instance).values_list('pk', flat=True))


@receiver(post_delete, sender=Employee)
def delete_login_with_employee(sender, instance, **kwargs):
    ids = getattr(instance, '_login_ids', None)
    if ids:
        User.objects.filter(pk__in=ids).delete()
