from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    AttendanceListView,
    AttendanceSummaryView,
    DeviceRegisterView,
    HealthView,
    MeView,
    NotificationListView,
)

app_name = 'api'

urlpatterns = [
    # Public health/liveness check (Phase 6 — mobile connectivity test)
    path('health/', HealthView.as_view(), name='health'),

    # Auth
    path('auth/login/', TokenObtainPairView.as_view(), name='login'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='refresh'),

    # Employee self-service
    path('me/', MeView.as_view(), name='me'),
    path('attendance/', AttendanceListView.as_view(), name='attendance'),
    path('attendance/summary/', AttendanceSummaryView.as_view(), name='attendance-summary'),
    path('notifications/', NotificationListView.as_view(), name='notifications'),
    path('devices/register/', DeviceRegisterView.as_view(), name='device-register'),
]
