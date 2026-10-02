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


def get_device_client(backend: str | None = None, ip: str | None = None,
                       port: int | None = None, **kwargs) -> BaseDeviceClient:
    """Build the configured backend's client.

    For the ``zk`` backend, ``ip``/``port`` default to the active
    :class:`~apps.devices.models.BiometricDevice` — the Devices page is the
    single source of truth for where the real unit lives; nothing is read from
    an env var. Pass ``ip`` explicitly to target a specific device regardless
    of which one is marked active (e.g. a per-device "Test connection" button).
    """
    key = (backend or settings.BIOMETRIC_DEVICE_BACKEND or 'mock').lower()
    try:
        dotted = _BACKENDS[key]
    except KeyError:
        raise ValueError(
            f"Unknown BIOMETRIC_DEVICE_BACKEND '{key}'. "
            f"Valid options: {', '.join(sorted(_BACKENDS))}."
        )

    if key == 'zk':
        if ip is None:
            from apps.devices.models import BiometricDevice

            device = BiometricDevice.objects.filter(is_active=True).first()
            if device is None:
                raise ValueError(
                    'No active biometric device configured. Add one on the '
                    'Devices page.')
            ip, port = device.ip_address, (port or device.port)
        kwargs['ip'] = ip
        if port is not None:
            kwargs['port'] = port

    module_path, _, class_name = dotted.rpartition('.')
    module = __import__(module_path, fromlist=[class_name])
    client_cls = getattr(module, class_name)
    return client_cls(**kwargs)
