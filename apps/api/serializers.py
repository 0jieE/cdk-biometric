from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.devices.models import MobileDevice
from apps.notifications.models import Notification

from .photos import normalize_photo
from .usernames import availability_problem, format_problem, normalize_username


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
    username = serializers.CharField(source='user.username', read_only=True)
    photo_url = serializers.SerializerMethodField()

    def get_photo_url(self, employee):
        """Absolute URL of the profile photo, or null if none has been set."""
        if not employee.photo:
            return None
        request = self.context.get('request')
        url = employee.photo.url
        return request.build_absolute_uri(url) if request else url


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


class PhotoUploadSerializer(serializers.Serializer):
    # Validated as an image and reduced to a small, metadata-free JPEG (bytes).
    photo = serializers.FileField()

    def validate_photo(self, value):
        return normalize_photo(value)


class ChangePasswordSerializer(serializers.Serializer):
    # Passwords are taken verbatim - no whitespace trimming.
    old_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_old_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Incorrect password.')
        return value

    def validate(self, attrs):
        # Only reached when the old password was right (field errors stop earlier),
        # so a wrong guess learns nothing about what the new one may be.
        user = self.context['request'].user
        new = attrs['new_password']
        if new == attrs['old_password']:
            raise serializers.ValidationError(
                {'new_password': ['The new password must be different from the current one.']})
        try:
            validate_password(new, user)       # the project's AUTH_PASSWORD_VALIDATORS
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'new_password': list(exc.messages)})
        return attrs


class ChangeUsernameSerializer(serializers.Serializer):
    username = serializers.CharField()
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Incorrect password.')
        return value

    def validate_username(self, value):
        value = normalize_username(value)
        problem = format_problem(value)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate(self, attrs):
        # Only reached once BOTH fields are individually valid, i.e. the password
        # was right. Availability is checked last so that someone holding a stolen
        # token (no password) can't use this endpoint to probe which names exist.
        problem = availability_problem(attrs['username'], self.context['request'].user)
        if problem:
            raise serializers.ValidationError({'username': [problem]})
        return attrs

