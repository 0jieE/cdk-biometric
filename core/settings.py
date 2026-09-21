"""
Django settings for the Biometric Attendance Monitoring System (Phase 1).

Configuration is read from the environment via django-environ (.env file).
See .env.example for the full list of supported keys.
"""

from datetime import timedelta
from pathlib import Path

import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ['127.0.0.1', 'localhost']),
    CORS_ALLOWED_ORIGINS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    DB_PORT=(int, 3306),
    ZK_DEVICE_PORT=(int, 4370),
    ZK_DEVICE_TIMEOUT=(int, 10),
    SYNC_INTERVAL_MINUTES=(int, 5),
)

# Read the .env file living next to manage.py (backend/.env).
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY', default='django-insecure-dev-only-change-me')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env('ALLOWED_HOSTS')


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_celery_beat',
]

LOCAL_APPS = [
    'apps.accounts',
    'apps.organization',
    'apps.devices',
    'apps.attendance',
    'apps.notifications',
    'apps.api',
    'apps.reports',
    'apps.webportal',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'


# ---------------------------------------------------------------------------
# Database (MySQL via mysqlclient)
# ---------------------------------------------------------------------------
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': env('DB_NAME', default='attendance_db'),
        'USER': env('DB_USER', default='root'),
        'PASSWORD': env('DB_PASSWORD', default='root'),
        'HOST': env('DB_HOST', default='127.0.0.1'),
        'PORT': env('DB_PORT'),
        'OPTIONS': {
            'charset': 'utf8mb4',
        },
    }
}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ---------------------------------------------------------------------------
# Internationalization / Timezone
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static / media files
# ---------------------------------------------------------------------------
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Web portal (session auth) login/redirect targets.
LOGIN_URL = 'webportal:login'
LOGIN_REDIRECT_URL = 'webportal:dashboard'
LOGOUT_REDIRECT_URL = 'webportal:login'

INSTITUTION_NAME = 'Colegio de Kidapawan'

# Public (no-login) live-logs kiosk page. Reachable by anyone with the URL —
# via the Cloudflare tunnel that means the public internet, exposing employee
# names. Set to False to disable the page entirely (returns 404).
LIVE_LOGS_PUBLIC = env.bool('LIVE_LOGS_PUBLIC', default=True)


# ---------------------------------------------------------------------------
# Django REST Framework + SimpleJWT
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    'DATETIME_FORMAT': 'iso-8601',
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': False,
    'AUTH_HEADER_TYPES': ('Bearer',),
    # Tolerate small clock skew. Docker Desktop's VM clock is periodically
    # re-synced (it can step BACKWARDS ~1s), so a token issued a moment ago can
    # look "issued in the future" to the same server and be rejected as
    # "Token is invalid" — a random forced logout in the mobile app. With no
    # leeway, even 1s of skew is fatal. 60s is negligible against the 60-minute
    # access-token lifetime.
    'LEEWAY': 60,
}


# ---------------------------------------------------------------------------
# CORS (for the Flutter app in Phase 3)
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env('CORS_ALLOWED_ORIGINS')


# ---------------------------------------------------------------------------
# Public exposure via Cloudflare Quick Tunnel (Phase 5)
# ---------------------------------------------------------------------------
# The random *.trycloudflare.com host is matched via a leading-dot entry in
# ALLOWED_HOSTS (set in .env / .env.docker). These let the CSRF-protected admin
# portal work over the tunnel, where Cloudflare terminates TLS and cloudflared
# forwards to nginx over http.
CSRF_TRUSTED_ORIGINS = env('CSRF_TRUSTED_ORIGINS')
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True


# ---------------------------------------------------------------------------
# Biometric device layer
# ---------------------------------------------------------------------------
# 'mock' for development (no hardware) | 'zk' for a real ZKTeco unit.
BIOMETRIC_DEVICE_BACKEND = env('BIOMETRIC_DEVICE_BACKEND', default='mock')
ZK_DEVICE_IP = env('ZK_DEVICE_IP', default='192.168.1.201')
ZK_DEVICE_PORT = env('ZK_DEVICE_PORT')
ZK_DEVICE_TIMEOUT = env('ZK_DEVICE_TIMEOUT')

# First day real attendance was recorded (YYYY-MM-DD). Days before it are never
# reported present/absent. Blank => auto (day of the earliest recorded punch).
ATTENDANCE_START_DATE = env('ATTENDANCE_START_DATE', default='')

# Admin password used by the seed_data command (printed on seed).
ADMIN_PASSWORD = env('ADMIN_PASSWORD', default='admin12345')


# ---------------------------------------------------------------------------
# Celery (async tasks + periodic sync)
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='redis://127.0.0.1:6379/0')
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default='redis://127.0.0.1:6379/1')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
# Run tasks inline (no broker) — enabled in tests via override_settings.
CELERY_TASK_ALWAYS_EAGER = env.bool('CELERY_TASK_ALWAYS_EAGER', default=False)
CELERY_TASK_EAGER_PROPAGATES = True
# Fail fast if the broker is unreachable so callers can fall back to inline
# execution instead of blocking on connection retries.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = False
CELERY_BROKER_CONNECTION_MAX_RETRIES = 0
CELERY_BROKER_TRANSPORT_OPTIONS = {'socket_connect_timeout': 2, 'socket_timeout': 2}

# How often Celery Beat triggers the mock/real device sync.
SYNC_INTERVAL_MINUTES = env('SYNC_INTERVAL_MINUTES')


# ---------------------------------------------------------------------------
# Firebase Cloud Messaging (push). Blank path => FCM disabled (no-op).
# ---------------------------------------------------------------------------
FIREBASE_CREDENTIALS = env('FIREBASE_CREDENTIALS', default='')


# ---------------------------------------------------------------------------
# Logging (surface device/attendance service warnings on the console)
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'loggers': {
        'apps': {
            'handlers': ['console'],
            'level': 'INFO',
        },
    },
}
