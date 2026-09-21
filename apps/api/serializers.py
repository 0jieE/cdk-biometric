from rest_framework import serializers

from apps.devices.models import MobileDevice
from apps.notifications.models import Notification


class EmployeeProfileSerializer(serializers.Serializer):
    employee_no = serializers.CharField()
    full_name = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    department = serializers.CharField(source='department.name')
    department_code = serializers.CharField(source='department.code')
    position = serializers.CharField(allow_blank=True)
    date_hired = serializers.DateField(allow_null=True)
    is_fulltime = serializers.BooleanField()
    employment_type = serializers.CharField()


# NOTE: the per-day attendance shape is variable (full-time carries AM/PM
# sessions + overtime; part-time carries a single in/out + incomplete flag), so
# the AttendanceListView returns the selector dicts directly and lets DRF encode
# the datetimes. See apps/api/selectors.build_daily_attendance for the shape.


class AttendanceSummarySerializer(serializers.Serializer):
    month = serializers.CharField()
    present = serializers.IntegerField()
    late = serializers.IntegerField()
    half_day = serializers.IntegerField()
    absent = serializers.IntegerField()
    overtime_minutes = serializers.IntegerField()
    # Computed by monthly_summary all along, but never exposed until now.
    late_minutes = serializers.IntegerField()
    undertime_minutes = serializers.IntegerField()
    lost_minutes = serializers.IntegerField()


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ('id', 'title', 'body', 'type', 'is_read', 'sent_at', 'created_at')
        read_only_fields = fields


class MobileDeviceRegisterSerializer(serializers.Serializer):
    # Plain serializer (not ModelSerializer) so re-registering an existing
    # token is an update, not a uniqueness error, and platform is case-tolerant.
    fcm_token = serializers.CharField(max_length=255)
    platform = serializers.CharField(max_length=16)

    def validate_platform(self, value):
        value = value.upper()
        if value not in MobileDevice.Platforms.values:
            raise serializers.ValidationError(
                f'platform must be one of {MobileDevice.Platforms.values}'
            )
        return value
