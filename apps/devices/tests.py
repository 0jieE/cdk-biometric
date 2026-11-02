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


class QuickCheckTests(TestCase):
    """quick_check() must answer fast - it's what a frequently-polling
    dashboard badge calls, unlike test_connection()'s full handshake."""

    def test_mock_backend_delegates_to_test_connection(self):
        self.assertTrue(MockDeviceClient().quick_check())

    def test_reaches_a_real_open_port(self):
        import socket

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        try:
            port = server.getsockname()[1]
            self.assertTrue(ZKDeviceClient(ip='127.0.0.1', port=port).quick_check(timeout=2))
        finally:
            server.close()

    def test_refused_port_is_false_within_the_timeout(self):
        import socket
        import time

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 0))
        free_port = s.getsockname()[1]
        s.close()  # nothing listens here now

        # Linux refuses instantly; Windows retries the SYN until the timeout.
        # Either way the answer is False and never later than `timeout`.
        start = time.monotonic()
        ok = ZKDeviceClient(ip='127.0.0.1', port=free_port).quick_check(timeout=0.5)
        elapsed = time.monotonic() - start

        self.assertFalse(ok)
        self.assertLess(elapsed, 1.5)

    def test_never_blocks_past_its_own_timeout(self):
        import time

        # A non-routable address (RFC 5737 TEST-NET) so the connect attempt
        # actually has to wait, proving quick_check respects `timeout` itself
        # rather than pyzk's connect()/test_tcp(), which hardcodes 10s.
        start = time.monotonic()
        ok = ZKDeviceClient(ip='192.0.2.1', port=4370).quick_check(timeout=1)
        elapsed = time.monotonic() - start

        self.assertFalse(ok)
        self.assertLess(elapsed, 3)   # generous margin, still far under the old 10s+
