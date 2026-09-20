from django.contrib import admin

from .models import BiometricDevice, MobileDevice


@admin.register(BiometricDevice)
class BiometricDeviceAdmin(admin.ModelAdmin):
    list_display = ('name', 'ip_address', 'port', 'location', 'is_active', 'last_synced_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'ip_address', 'serial_no')


@admin.register(MobileDevice)
class MobileDeviceAdmin(admin.ModelAdmin):
    list_display = ('employee', 'platform', 'is_active', 'last_seen_at')
    list_filter = ('platform', 'is_active')
    search_fields = ('employee__employee_no', 'fcm_token')
