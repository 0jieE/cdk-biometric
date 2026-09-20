from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('employee', 'title', 'body', 'type', 'status', 'attempts',
                    'sent_at', 'created_at')
    list_filter = ('status', 'type', 'is_read')
    search_fields = ('employee__employee_no', 'title', 'body')
    date_hierarchy = 'created_at'
    readonly_fields = ('attempts', 'last_error', 'sent_at', 'created_at')
