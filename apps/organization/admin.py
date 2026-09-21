from django.contrib import admin

from .models import (
    Department,
    Employee,
    EmployeeSchedule,
    GlobalSchedule,
    Holiday,
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('code', 'name')


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        'employee_no', 'full_name', 'department', 'position',
        'is_fulltime', 'biometric_id', 'is_active',
    )
    list_filter = ('is_active', 'is_fulltime', 'department')
    search_fields = ('employee_no', 'first_name', 'last_name', 'biometric_id')
    autocomplete_fields = ('department',)


@admin.register(GlobalSchedule)
class GlobalScheduleAdmin(admin.ModelAdmin):
    list_display = ('am_in', 'am_out', 'pm_in', 'pm_out',
                    'midpoint', 'grace_period_minutes', 'workdays')

    def has_add_permission(self, request):
        # Singleton — only ever one row.
        return not GlobalSchedule.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmployeeSchedule)
class EmployeeScheduleAdmin(admin.ModelAdmin):
    list_display = ('employee', 'am_in', 'am_out', 'pm_in', 'pm_out',
                    'midpoint', 'grace_period_minutes', 'workdays')
    search_fields = ('employee__employee_no', 'employee__last_name')
    autocomplete_fields = ('employee',)


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ('date', 'name', 'type', 'is_recurring')
    list_filter = ('type', 'is_recurring')
    search_fields = ('name',)
    date_hierarchy = 'date'
