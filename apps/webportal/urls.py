from django.urls import path

from . import views

app_name = 'webportal'

urlpatterns = [
    # Auth
    path('', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Dashboard
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/recent/', views.dashboard_recent, name='dashboard_recent'),
    path('sync-now/', views.sync_now, name='sync_now'),

    # Attendance / tardiness / absences
    path('attendance/', views.attendance, name='attendance'),
    path('attendance/data/', views.attendance_data, name='attendance_data'),
    path('lates/', views.lates, name='lates'),
    path('absences/', views.absences, name='absences'),

    # Employees
    path('employees/', views.employees, name='employees'),
    path('employees/new/', views.employee_form, name='employee_create'),
    path('employees/<int:pk>/', views.employee_detail, name='employee_detail'),
    path('employees/<int:pk>/edit/', views.employee_form, name='employee_edit'),
    path('employees/<int:pk>/account/', views.employee_account, name='employee_account'),
    path('employees/<int:pk>/deactivate/', views.employee_deactivate, name='employee_deactivate'),

    # Departments
    path('departments/', views.departments, name='departments'),
    path('departments/new/', views.department_form, name='department_create'),
    path('departments/<int:pk>/', views.department_detail, name='department_detail'),
    path('departments/<int:pk>/edit/', views.department_form, name='department_edit'),

    # Holidays
    path('holidays/', views.holidays, name='holidays'),
    path('holidays/all/', views.holiday_list, name='holiday_list'),
    path('holidays/import/', views.holiday_import, name='holiday_import'),
    path('holidays/new/', views.holiday_form, name='holiday_create'),
    path('holidays/<int:pk>/edit/', views.holiday_form, name='holiday_edit'),
    path('holidays/<int:pk>/delete/', views.holiday_delete, name='holiday_delete'),

    # Devices
    path('devices/', views.devices, name='devices'),
    path('devices/new/', views.device_form, name='device_create'),
    path('devices/<int:pk>/edit/', views.device_form, name='device_edit'),
    path('devices/<int:pk>/sync/', views.device_sync, name='device_sync'),
    path('devices/<int:pk>/test/', views.device_test, name='device_test'),

    # Reports
    path('reports/', views.reports, name='reports'),
    path('reports/jobs/', views.report_jobs, name='report_jobs'),
    path('export/', views.export_report, name='export'),

    # Schedule settings (global + per-employee overrides)
    path('schedule/', views.schedule_settings, name='schedule'),
    path('schedule/list/', views.employee_schedule_list, name='employee_schedule_list'),
    path('schedule/employee/<int:pk>/', views.employee_schedule_form, name='employee_schedule'),
    path('schedule/employee/<int:pk>/clear/', views.employee_schedule_clear, name='employee_schedule_clear'),

    # Overtime authorization
    path('overtime/', views.overtime, name='overtime'),
    path('overtime/new/', views.overtime_form, name='overtime_create'),
    path('overtime/<int:pk>/revoke/', views.overtime_revoke, name='overtime_revoke'),

    # Manual attendance (brownout fallback)
    path('manual-attendance/', views.manual_attendance, name='manual_attendance'),

    # Live logs — PUBLIC (no login)
    path('live/', views.live_logs, name='live'),
    path('live/feed/', views.live_logs_feed, name='live_feed'),
    path('live/stream/', views.live_logs_stream, name='live_stream'),
]
