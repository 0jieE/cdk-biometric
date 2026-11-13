from django.test import TestCase
from apps.accounts.models import User

# Create your tests here.



class EmployeeLoginDeletionTests(TestCase):
    """Deleting an employee's login deletes the employee and all their records,
    and deleting the employee deletes their login."""

    def setUp(self):
        from datetime import date
        from django.utils import timezone
        from apps.attendance.models import Absence, AttendanceLog
        from apps.organization.models import Department, Employee
        dept = Department.objects.create(name='IT', code='IT')
        self.emp = Employee.objects.create(employee_no='D-1', first_name='Del', last_name='Me',
                                           department=dept, biometric_id='7701')
        self.user = User.objects.create(username='d-1', employee=self.emp)
        AttendanceLog.objects.create(employee=self.emp, log_type='AM_IN', log_datetime=timezone.now())
        Absence.objects.create(employee=self.emp, date=date(2026, 11, 2), minutes_absent=480)

    def _gone(self):
        from apps.attendance.models import Absence, AttendanceLog
        from apps.organization.models import Employee
        return (not Employee.objects.filter(pk=self.emp.pk).exists()
                and not User.objects.filter(pk=self.user.pk).exists()
                and not AttendanceLog.objects.filter(employee_id=self.emp.pk).exists()
                and not Absence.objects.filter(employee_id=self.emp.pk).exists())

    def test_deleting_the_login_deletes_the_employee_and_records(self):
        self.user.delete()
        self.assertTrue(self._gone())

    def test_deleting_the_employee_deletes_the_login(self):
        self.emp.delete()
        self.assertTrue(self._gone())

    def test_bulk_delete_in_admin_works_too(self):
        User.objects.filter(pk=self.user.pk).delete()
        self.assertTrue(self._gone())

    def test_an_admin_without_an_employee_is_unaffected(self):
        from apps.organization.models import Employee
        User.objects.create(username='plain-admin').delete()
        self.assertTrue(Employee.objects.filter(pk=self.emp.pk).exists())
