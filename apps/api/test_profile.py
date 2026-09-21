"""Profile photo (fetch / upload) and password-change endpoints."""

import shutil
import tempfile
from io import BytesIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from apps.organization.models import Department, Employee, GlobalSchedule

User = get_user_model()

PASSWORD = 'Str0ng-Start-Pass'
NEW_PASSWORD = 'Another-Str0ng-Pass'

_MEDIA = tempfile.mkdtemp(prefix='cdk-test-media-')


def tearDownModule():
    shutil.rmtree(_MEDIA, ignore_errors=True)


def image_file(fmt='JPEG', size=(800, 600), mode='RGB', name=None, exif=None):
    """An in-memory image upload."""
    buf = BytesIO()
    kwargs = {'exif': exif.tobytes()} if exif is not None else {}
    Image.new(mode, size, (10, 120, 200) if mode == 'RGB' else 0).save(buf, fmt, **kwargs)
    ext = {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}[fmt]
    return SimpleUploadedFile(name or f'me.{ext}', buf.getvalue(),
                              content_type=f'image/{ext}')


class _ProfileBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        GlobalSchedule.load()
        dept = Department.objects.create(name='IT', code='IT')
        cls.emp_a = Employee.objects.create(
            employee_no='EMP-A', first_name='Alice', last_name='Adams',
            department=dept, biometric_id='6001', is_fulltime=True)
        cls.emp_b = Employee.objects.create(
            employee_no='EMP-B', first_name='Bob', last_name='Brown',
            department=dept, biometric_id='6002', is_fulltime=False)
        cls.user_a = cls._user('alice', cls.emp_a)
        cls.user_b = cls._user('bob', cls.emp_b)

    @staticmethod
    def _user(username, emp):
        user = User.objects.create(username=username, role=User.Roles.EMPLOYEE, employee=emp)
        user.set_password(PASSWORD)
        user.save()
        return user

    def setUp(self):
        cache.clear()                         # the password endpoint is throttled
        self._media = override_settings(MEDIA_ROOT=_MEDIA)
        self._media.enable()
        self.addCleanup(self._media.disable)
        self.addCleanup(lambda: shutil.rmtree(Path(_MEDIA) / 'employees', ignore_errors=True))

    def _tokens(self, username='alice', password=PASSWORD):
        resp = APIClient().post('/api/v1/auth/login/',
                                {'username': username, 'password': password}, format='json')
        return resp

    def _login(self, username='alice'):
        resp = self._tokens(username)
        self.assertEqual(resp.status_code, 200, resp.content)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access"]}')
        return client

    def _upload(self, client, upload, method='post', **extra):
        return getattr(client, method)('/api/v1/me/photo/', {'photo': upload},
                                       format='multipart', **extra)

    def _stored_files(self):
        folder = Path(_MEDIA) / 'employees'
        return sorted(p.name for p in folder.glob('*')) if folder.exists() else []


class PhotoFetchAndUploadTests(_ProfileBase):
    def test_me_reports_null_photo_url_before_any_upload(self):
        resp = self._login().get('/api/v1/me/')
        self.assertIn('photo_url', resp.data)
        self.assertIsNone(resp.data['photo_url'])

    def test_upload_returns_a_url_that_me_then_reports(self):
        client = self._login()
        resp = self._upload(client, image_file())
        self.assertEqual(resp.status_code, 200, resp.content)
        url = resp.data['photo_url']
        self.assertTrue(url.startswith('http://testserver/media/employees/emp-a-'), url)
        self.assertTrue(url.endswith('.jpg'))
        self.assertEqual(client.get('/api/v1/me/').data['photo_url'], url)
        self.assertEqual(len(self._stored_files()), 1)

    def test_photo_url_is_https_behind_a_tunnel(self):
        # nginx now passes the tunnel's X-Forwarded-Proto through; Django must use it,
        # otherwise the phone is handed an http:// URL that Android refuses to load.
        client = self._login()
        with override_settings(ALLOWED_HOSTS=['*']):
            self._upload(client, image_file())
            url = client.get('/api/v1/me/', HTTP_HOST='demo.ngrok-free.dev',
                             HTTP_X_FORWARDED_PROTO='https').data['photo_url']
        self.assertTrue(url.startswith('https://demo.ngrok-free.dev/media/employees/'), url)

    def test_large_png_is_stored_as_a_small_jpeg(self):
        client = self._login()
        self._upload(client, image_file('PNG', size=(2400, 1800), mode='RGBA', name='big.png'))
        (name,) = self._stored_files()
        path = Path(_MEDIA) / 'employees' / name
        with Image.open(path) as stored:
            self.assertEqual(stored.format, 'JPEG')
            self.assertLessEqual(max(stored.size), 512)
        self.assertLess(path.stat().st_size, 100 * 1024)

    def test_exif_metadata_is_stripped_and_orientation_applied(self):
        exif = Image.Exif()
        exif[0x010F] = 'TestCam'
        exif[0x0112] = 6                                   # phone held sideways
        exif[0x8825] = {1: 'N', 2: (14.0, 35.0, 0.0), 3: 'E', 4: (125.0, 3.0, 0.0)}   # GPS
        upload = image_file(size=(800, 400), exif=exif)
        with Image.open(BytesIO(upload.read())) as sent:
            self.assertTrue(sent.getexif().get_ifd(0x8825), 'test image should carry GPS')
        upload.seek(0)

        self._upload(self._login(), upload)
        (name,) = self._stored_files()
        with Image.open(Path(_MEDIA) / 'employees' / name) as stored:
            self.assertEqual(len(stored.getexif()), 0, 'EXIF must not be stored')
            self.assertFalse(stored.getexif().get_ifd(0x8825), 'GPS must not be stored')
            self.assertGreater(stored.height, stored.width)   # rotated upright: 800x400 -> portrait

    def test_replacing_the_photo_deletes_the_old_file(self):
        client = self._login()
        first = self._upload(client, image_file()).data['photo_url']
        (first_name,) = self._stored_files()
        second = self._upload(client, image_file(size=(300, 300))).data['photo_url']
        self.assertNotEqual(first, second)                 # fresh URL => no stale cache
        files = self._stored_files()
        self.assertEqual(len(files), 1)
        self.assertNotEqual(files[0], first_name)

    def test_put_behaves_like_post(self):
        resp = self._upload(self._login(), image_file(), method='put')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('photo_url', resp.data)

    def test_one_employee_cannot_change_anothers_photo(self):
        self._upload(self._login('bob'), image_file())
        self.assertIsNone(self._login('alice').get('/api/v1/me/').data['photo_url'])
        self.assertIsNotNone(self._login('bob').get('/api/v1/me/').data['photo_url'])

    def test_a_photo_set_in_the_portal_is_exposed_too(self):
        Employee.objects.filter(pk=self.emp_a.pk).update(photo='employees/portal-set.jpg')
        url = self._login().get('/api/v1/me/').data['photo_url']
        self.assertEqual(url, 'http://testserver/media/employees/portal-set.jpg')

    # -- rejected uploads -----------------------------------------------------------
    def test_non_image_is_rejected(self):
        junk = SimpleUploadedFile('me.jpg', b'this is definitely not a picture',
                                  content_type='image/jpeg')
        resp = self._upload(self._login(), junk)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('photo', resp.data)
        self.assertEqual(self._stored_files(), [])

    def test_unsupported_format_is_rejected(self):
        resp = self._upload(self._login(), image_file('GIF'))
        self.assertEqual(resp.status_code, 400)

    def test_oversized_upload_is_rejected(self):
        big = SimpleUploadedFile('me.jpg', b'\xff\xd8' + b'0' * (10 * 1024 * 1024),
                                 content_type='image/jpeg')
        resp = self._upload(self._login(), big)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('too large', str(resp.data['photo']))

    def test_a_large_phone_jpeg_is_accepted_cheaply(self):
        # 24 megapixels (a normal high-end phone shot) must work, and come out small.
        resp = self._upload(self._login(), image_file(size=(6000, 4000), name='IMG_9999.jpg'))
        self.assertEqual(resp.status_code, 200, resp.content)
        (name,) = self._stored_files()
        with Image.open(Path(_MEDIA) / 'employees' / name) as stored:
            self.assertLessEqual(max(stored.size), 512)
            self.assertEqual(stored.size, (512, 341))            # aspect ratio kept

    def test_a_48_megapixel_jpeg_is_accepted_but_a_48mp_png_is_not(self):
        # JPEG is decoded at reduced scale so it can be big; PNG is decoded in full,
        # so its cap is lower (this is the pixel-bomb guard for non-JPEG formats).
        client = self._login()
        self.assertEqual(self._upload(client, image_file(size=(8000, 6000))).status_code, 200)
        bomb = image_file('PNG', size=(8000, 6000), mode='1', name='bomb.png')
        self.assertEqual(self._upload(client, bomb).status_code, 400)

    def test_pixel_bomb_is_rejected_from_the_header_alone(self):
        # A few KB on the wire that claims 48 megapixels: must be refused without
        # decoding it (that decode is what exhausts memory).
        bomb = image_file('PNG', size=(8000, 6000), mode='1', name='bomb.png')
        self.assertLess(bomb.size, 50 * 1024)
        resp = self._upload(self._login(), bomb)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('dimensions', str(resp.data['photo']))

    def test_missing_or_empty_photo_field_is_rejected(self):
        client = self._login()
        self.assertEqual(client.post('/api/v1/me/photo/', {}, format='multipart').status_code, 400)
        empty = SimpleUploadedFile('me.jpg', b'', content_type='image/jpeg')
        self.assertEqual(self._upload(client, empty).status_code, 400)

    def test_json_body_is_not_accepted(self):
        resp = self._login().post('/api/v1/me/photo/', {'photo': 'x'}, format='json')
        self.assertEqual(resp.status_code, 415)

    def test_authentication_and_role_required(self):
        self.assertEqual(self._upload(APIClient(), image_file()).status_code, 401)
        admin = User.objects.create(username='root', role=User.Roles.ADMIN,
                                    is_staff=True, is_superuser=True)
        admin.set_password(PASSWORD)
        admin.save()
        self.assertEqual(self._upload(self._login('root'), image_file()).status_code, 403)


class ChangePasswordTests(_ProfileBase):
    URL = '/api/v1/me/password/'

    def _change(self, client, old=PASSWORD, new=NEW_PASSWORD):
        return client.post(self.URL, {'old_password': old, 'new_password': new}, format='json')

    def test_success_returns_fresh_tokens_and_switches_the_password(self):
        client = self._login()
        resp = self._change(client)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data['detail'], 'password changed')

        # The new tokens work on this device...
        fresh = APIClient()
        fresh.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access"]}')
        self.assertEqual(fresh.get('/api/v1/me/').status_code, 200)
        refreshed = APIClient().post('/api/v1/auth/refresh/',
                                     {'refresh': resp.data['refresh']}, format='json')
        self.assertEqual(refreshed.status_code, 200)
        # ...and only the new password logs in from now on.
        self.assertEqual(self._tokens(password=NEW_PASSWORD).status_code, 200)
        self.assertEqual(self._tokens(password=PASSWORD).status_code, 401)

    def test_every_earlier_token_is_revoked(self):
        # Two other sessions (a second phone / a stolen token), issued before the change.
        other = self._tokens().data
        stale_access = APIClient()
        stale_access.credentials(HTTP_AUTHORIZATION=f'Bearer {other["access"]}')
        self.assertEqual(stale_access.get('/api/v1/me/').status_code, 200)

        self._change(self._login())

        self.assertEqual(stale_access.get('/api/v1/me/').status_code, 401)
        refresh = APIClient().post('/api/v1/auth/refresh/',
                                   {'refresh': other['refresh']}, format='json')
        self.assertEqual(refresh.status_code, 401)
        self.assertEqual(refresh.data['code'], 'password_changed')

    def test_a_portal_password_reset_also_signs_the_phone_out(self):
        client = self._login()
        self.assertEqual(client.get('/api/v1/me/').status_code, 200)
        self.user_a.set_password('employee12345')      # what the portal's "reset" does
        self.user_a.save()
        self.assertEqual(client.get('/api/v1/me/').status_code, 401)

    def test_refresh_still_works_normally(self):
        tokens = self._tokens().data
        resp = APIClient().post('/api/v1/auth/refresh/', {'refresh': tokens['refresh']},
                                format='json')
        self.assertEqual(resp.status_code, 200)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access"]}')
        self.assertEqual(client.get('/api/v1/me/').status_code, 200)

    def test_garbage_refresh_token_is_rejected(self):
        resp = APIClient().post('/api/v1/auth/refresh/', {'refresh': 'not.a.token'}, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_wrong_current_password_is_rejected_and_nothing_changes(self):
        resp = self._change(self._login(), old='wrong-password')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('old_password', resp.data)
        self.assertNotIn('new_password', resp.data)        # reveals nothing about the new one
        self.assertEqual(self._tokens().status_code, 200)  # still the original password

    def test_new_password_must_pass_the_project_rules(self):
        client = self._login()
        # too common, all-numeric-ish, too short, too like the username, unchanged
        responses = {}
        for bad in ('password', '12345678', 'short', 'alice123', PASSWORD):   # 5 = the throttle cap
            responses[bad] = self._change(client, new=bad)
            self.assertEqual(responses[bad].status_code, 400, bad)
            self.assertIn('new_password', responses[bad].data, bad)
        self.assertIn('similar to the username', ' '.join(responses['alice123'].data['new_password']))
        self.assertEqual(self._tokens().status_code, 200)  # never changed

    def test_missing_fields_are_rejected(self):
        resp = self._login().post(self.URL, {}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(set(resp.data), {'old_password', 'new_password'})

    def test_attempts_are_throttled(self):
        client = self._login()
        codes = [self._change(client, old='wrong-password').status_code for _ in range(6)]
        self.assertEqual(codes, [400] * 5 + [429])

    def test_authentication_and_role_required(self):
        self.assertEqual(APIClient().post(self.URL, {}, format='json').status_code, 401)
        admin = User.objects.create(username='root', role=User.Roles.ADMIN,
                                    is_staff=True, is_superuser=True)
        admin.set_password(PASSWORD)
        admin.save()
        self.assertEqual(self._change(self._login('root')).status_code, 403)
