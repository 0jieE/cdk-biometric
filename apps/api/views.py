from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.devices.models import MobileDevice
from apps.notifications.models import Notification

from .permissions import IsEmployeeUser
from .selectors import build_daily_attendance, monthly_summary
from .serializers import (
    AttendanceSummarySerializer,
    EmployeeProfileSerializer,
    MobileDeviceRegisterSerializer,
    NotificationSerializer,
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
        serializer = EmployeeProfileSerializer(request.user.employee)
        return Response(serializer.data)


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
