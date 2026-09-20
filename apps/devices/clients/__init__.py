"""Pluggable biometric-device client layer.

Nothing outside this package should import a concrete client. Always resolve
one through :func:`get_device_client`, which honours the
``BIOMETRIC_DEVICE_BACKEND`` setting ('mock' | 'zk').
"""

from .base import BaseDeviceClient, RawPunch, RawUser
from .factory import get_device_client

__all__ = [
    'BaseDeviceClient',
    'RawPunch',
    'RawUser',
    'get_device_client',
]
