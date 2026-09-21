import logging
import uuid
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.devices.models import MobileDevice
from apps.notifications.models import Notification

from .permissions import IsEmployeeUser
from .usernames import UNAVAILABLE_MESSAGE

logger = logging.getLogger('apps.api')
from .selectors import build_daily_attendance, monthly_summary
from .serializers import (
    AttendanceSummarySerializer,
    ChangePasswordSerializer,
    ChangeUsernameSerializer,
    EmployeeProfileSerializer,
    MobileDeviceRegisterSerializer,
    NotificationSerializer,
    PhotoUploadSerializer,
)


def _parse_date(value):
    """Parse a YYYY-MM-DD string, returning a date or None."""
    if not value:
        return None
    try:
        return timezone.datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


class HealthView(APIView):
    """Public, unauthenticated liveness check used by the mobile app to verify
    connectivity and confirm it is talking to the right server (Phase 6)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'status': 'ok', 'service': 'attendance-api'})


class MeView(APIView):
    permission_classes = [IsEmployeeUser]

    def get(self, request):
        serializer = EmployeeProfileSerializer(
            request.user.employee, context={'request': request})
        return Response(serializer.data)


class MePhotoView(APIView):
    """Upload (or replace) the signed-in employee's profile photo.

    multipart/form-data with a ``photo`` file. The image is validated, made
    upright, shrunk to <=512px and re-encoded as a metadata-free JPEG (so EXIF GPS
    never reaches disk). The previous file is deleted. Returns the new URL, also
    exposed as ``photo_url`` on GET /me/.
    """

    permission_classes = [IsEmployeeUser]
    parser_classes = [MultiPartParser]

    def post(self, request):
        serializer = PhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        jpeg = serializer.validated_data['photo']

        employee = request.user.employee
        old_name = employee.photo.name if employee.photo else None
        # Random suffix: every upload gets a fresh URL (phones/caches never show a
        # stale photo) and the path isn't guessable from the employee number alone.
        filename = f'{slugify(employee.employee_no)}-{uuid.uuid4().hex[:12]}.jpg'
        employee.photo.save(filename, ContentFile(jpeg), save=False)
        employee.save(update_fields=['photo', 'updated_at'])
        if old_name and old_name != employee.photo.name:
            employee.photo.storage.delete(old_name)

        return Response({'photo_url': request.build_absolute_uri(employee.photo.url)})

    put = post


class ChangeUsernameView(APIView):
    """Change the signed-in employee's username (their login name).

    Body: ``username`` and ``current_password``. The password is required because
    a username is a login credential — without it a stolen access token could lock
    the real owner out by renaming the account. Names are normalised to lowercase;
    taken, reserved and other-employee-number names are refused. Tokens identify
    the user by id, so nobody is signed out. Shares the password-change throttle
    (5/min) since it also verifies the current password.
    """

    permission_classes = [IsEmployeeUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'password'

    def post(self, request):
        serializer = ChangeUsernameSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        old = user.username
        user.username = serializer.validated_data['username']
        try:
            user.save(update_fields=['username', 'updated_at'])
        except IntegrityError:          # lost a race for the same name
            return Response({'username': [UNAVAILABLE_MESSAGE]},
                            status=status.HTTP_400_BAD_REQUEST)
        logger.info('username changed for user id=%s: %r -> %r', user.pk, old, user.username)
        return Response({'username': user.username})

    put = patch = post


class ChangePasswordView(APIView):
    """Change the signed-in employee's password.

    Body: ``old_password`` and ``new_password`` (checked against the project's
    password rules). Every token issued before the change stops working, so the
    response carries a fresh ``access``/``refresh`` pair for THIS device. Throttled
    (5/min) because it verifies the current password.
    """

    permission_classes = [IsEmployeeUser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'password'

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.save(update_fields=['password'])

        refresh = RefreshToken.for_user(user)        # signed with the NEW password hash
        return Response({
            'detail': 'password changed',
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        })


class AttendanceListView(APIView):
    """Paired IN/OUT logs per day with computed ON_TIME/LATE/ABSENT status.

    Query params: ?start=YYYY-MM-DD&end=YYYY-MM-DD (default: last 30 days).
    """

    permission_classes = [IsEmployeeUser]

    def get(self, request):
        today = timezone.localdate()
        start = _parse_date(request.query_params.get('start')) or (today - timedelta(days=30))
        end = _parse_date(request.query_params.get('end')) or today
        if start > end:
            return Response(
                {'detail': 'start must be on or before end.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        records = build_daily_attendance(request.user.employee, start, end)
        records.sort(key=lambda r: r['date'], reverse=True)

        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(records, request, view=self)
        # Rich, type-dependent dicts (datetimes encoded by DRF's JSON renderer).
        return paginator.get_paginated_response(page)


class AttendanceSummaryView(APIView):
    """Monthly present/late/absent counts. ?month=YYYY-MM (default: this month)."""

    permission_classes = [IsEmployeeUser]

    def get(self, request):
        today = timezone.localdate()
        month_param = request.query_params.get('month')
        year, month = today.year, today.month
        if month_param:
            try:
                year, month = (int(p) for p in month_param.split('-'))
                if not 1 <= month <= 12:
                    raise ValueError
            except (ValueError, TypeError):
                return Response(
                    {'detail': 'month must be YYYY-MM.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        data = monthly_summary(request.user.employee, year, month)
        return Response(AttendanceSummarySerializer(data).data)


class NotificationListView(ListAPIView):
    permission_classes = [IsEmployeeUser]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(employee=self.request.user.employee)


class DeviceRegisterView(APIView):
    """Register or update this phone's FCM token (Phase 3 push)."""

    permission_classes = [IsEmployeeUser]

    def post(self, request):
        serializer = MobileDeviceRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device, created = MobileDevice.objects.update_or_create(
            fcm_token=serializer.validated_data['fcm_token'],
            defaults={
                'employee': request.user.employee,
                'platform': serializer.validated_data['platform'],
                'is_active': True,
                'last_seen_at': timezone.now(),
            },
        )
        return Response(
            {'detail': 'registered', 'created': created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
