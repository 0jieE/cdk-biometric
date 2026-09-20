from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ('username', 'email', 'role', 'employee', 'is_staff', 'is_active')
    list_filter = ('role', 'is_staff', 'is_active')
    search_fields = ('username', 'email', 'employee__employee_no')
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('Attendance system', {'fields': ('role', 'employee')}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ('Attendance system', {'fields': ('role', 'employee')}),
    )
