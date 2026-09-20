from rest_framework.permissions import BasePermission


class IsEmployeeUser(BasePermission):
    """Allow only authenticated EMPLOYEE users that are linked to an Employee.

    Because every endpoint derives its data from ``request.user.employee`` and
    never accepts an employee id from the client, this permission is what keeps
    one employee from ever reaching another employee's records.
    """

    message = 'This endpoint is only available to employee accounts.'

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, 'is_employee', False)
            and user.employee_id is not None
        )
