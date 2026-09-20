"""Factory that resolves the configured biometric backend.

Everything else must go through :func:`get_device_client` — never import a
concrete client directly, so switching hardware is a one-line env change.
"""

from __future__ import annotations

from django.conf import settings

from .base import BaseDeviceClient

_BACKENDS = {
    'mock': 'apps.devices.clients.mock_client.MockDeviceClient',
    'zk': 'apps.devices.clients.zk_client.ZKDeviceClient',
}


def get_device_client(backend: str | None = None, **kwargs) -> BaseDeviceClient:
    key = (backend or settings.BIOMETRIC_DEVICE_BACKEND or 'mock').lower()
    try:
        dotted = _BACKENDS[key]
    except KeyError:
        raise ValueError(
            f"Unknown BIOMETRIC_DEVICE_BACKEND '{key}'. "
            f"Valid options: {', '.join(sorted(_BACKENDS))}."
        )

    module_path, _, class_name = dotted.rpartition('.')
    module = __import__(module_path, fromlist=[class_name])
    client_cls = getattr(module, class_name)
    return client_cls(**kwargs)
