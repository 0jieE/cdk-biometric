from django.contrib import admin

from .models import ReportJob


@admin.register(ReportJob)
class ReportJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'report_type', 'fmt', 'status', 'requested_by',
                    'created_at', 'finished_at')
    list_filter = ('report_type', 'fmt', 'status')
    search_fields = ('requested_by__username',)
    readonly_fields = ('created_at', 'finished_at')
