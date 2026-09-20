from django.contrib import admin

from .models import Absence, AttendanceLog, Lates, OTAuthorization, Overtime


@admin.register(AttendanceLog)
class AttendanceLogAdmin(admin.ModelAdmin):
    list_display = ('employee', 'log_type', 'log_datetime', 'device', 'source', 'created_by')
    list_filter = ('log_type', 'source', 'device')
    search_fields = ('employee__employee_no', 'employee__last_name')
    date_hierarchy = 'log_datetime'


@admin.register(Lates)
class LatesAdmin(admin.ModelAdmin):
    list_display = ('employee', 'date', 'session', 'minutes_late', 'attendance_log')
    list_filter = ('session', 'date')
    search_fields = ('employee__employee_no', 'employee__last_name')
    date_hierarchy = 'date'


@admin.register(Absence)
class AbsenceAdmin(admin.ModelAdmin):
    list_display = ('employee', 'date', 'session', 'reason', 'incomplete')
    list_filter = ('session', 'reason', 'incomplete', 'date')
    search_fields = ('employee__employee_no', 'employee__last_name')
    date_hierarchy = 'date'


@admin.register(OTAuthorization)
class OTAuthorizationAdmin(admin.ModelAdmin):
    list_display = ('employee', 'date', 'ot_start', 'ot_end_expected', 'approved_by')
    list_filter = ('date',)
    search_fields = ('employee__employee_no', 'employee__last_name')
    date_hierarchy = 'date'


@admin.register(Overtime)
class OvertimeAdmin(admin.ModelAdmin):
    list_display = ('employee', 'date', 'minutes', 'authorization')
    list_filter = ('date',)
    search_fields = ('employee__employee_no', 'employee__last_name')
    date_hierarchy = 'date'
