"""The real device's IP/port must come from the Devices page (BiometricDevice),
never from an env var - so changing hardware is just editing that page."""

from django.test import TestCase, override_settings

from apps.devices.clients import get_device_client
from apps.devices.clients.mock_client import MockDeviceClient
from apps.devices.clients.zk_client import ZKDeviceClient
from apps.devices.models import BiometricDevice


@override_settings(BIOMETRIC_DEVICE_BACKEND='zk')
class GetDeviceClientZkTests(TestCase):
    def test_uses_the_active_device_s_address(self):
        BiometricDevice.objects.create(
            name='Lobby', ip_address='192.168.1.3', port=4370, is_active=True)
        client = get_device_client()
        self.assertIsInstance(client, ZKDeviceClient)
        self.assertEqual(client.ip, '192.168.1.3')
        self.assertEqual(client.port, 4370)

    def test_ignores_an_inactive_device(self):
        BiometricDevice.objects.create(
            name='Retired', ip_address='192.168.1.9', port=4370, is_active=False)
        with self.assertRaises(ValueError):
            get_device_client()

    def test_no_device_at_all_raises_clearly(self):
        with self.assertRaises(ValueError):
            get_device_client()

    def test_explicit_ip_overrides_the_active_device(self):
        # Used by the Devices page's per-device "Test connection" button, which
        # must check the device the admin clicked, not whichever is "active".
        BiometricDevice.objects.create(
            name='Lobby', ip_address='192.168.1.3', port=4370, is_active=True)
        client = get_device_client(ip='192.168.1.9', port=9999)
        self.assertEqual((client.ip, client.port), ('192.168.1.9', 9999))

    def test_changing_the_device_s_ip_takes_effect_immediately(self):
        # No env var, no restart - just editing the row, as the Devices page does.
        device = BiometricDevice.objects.create(
            name='Lobby', ip_address='192.168.1.3', port=4370, is_active=True)
        device.ip_address = '192.168.1.55'
        device.save()
        self.assertEqual(get_device_client().ip, '192.168.1.55')


class MockBackendUnaffectedTests(TestCase):
    @override_settings(BIOMETRIC_DEVICE_BACKEND='mock')
    def test_mock_backend_never_needs_a_device_row(self):
        self.assertFalse(BiometricDevice.objects.exists())
        self.assertIsInstance(get_device_client(), MockDeviceClient)


class ZKDeviceClientTests(TestCase):
    def test_requires_an_ip(self):
        with self.assertRaises(ValueError):
            ZKDeviceClient(ip=None)

    def test_port_defaults_without_settings(self):
        self.assertEqual(ZKDeviceClient(ip='192.168.1.3').port, 4370)
