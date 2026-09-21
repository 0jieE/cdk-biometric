from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView

from .auth import RevokeAwareTokenRefreshView

from .views import (
    AttendanceListView,
    AttendanceSummaryView,
    ChangePasswordView,
    ChangeUsernameView,
    DeviceRegisterView,
    HealthView,
    MePhotoView,
    MeView,
    NotificationListView,
)

app_name = 'api'

urlpatterns = [
    # Public health/liveness check (Phase 6 — mobile connectivity test)
    path('health/', HealthView.as_view(), name='health'),

    # Auth
    path('auth/login/', TokenObtainPairView.as_view(), name='login'),
    path('auth/refresh/', RevokeAwareTokenRefreshView.as_view(), name='refresh'),

    # Employee self-service
    path('me/', MeView.as_view(), name='me'),
    path('me/photo/', MePhotoView.as_view(), name='me-photo'),
    path('me/password/', ChangePasswordView.as_view(), name='me-password'),
    path('me/username/', ChangeUsernameView.as_view(), name='me-username'),
    path('attendance/', AttendanceListView.as_view(), name='attendance'),
    path('attendance/summary/', AttendanceSummaryView.as_view(), name='attendance-summary'),
    path('notifications/', NotificationListView.as_view(), name='notifications'),
    path('devices/register/', DeviceRegisterView.as_view(), name='device-register'),
]
