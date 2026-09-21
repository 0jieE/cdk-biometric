"""Employee-initiated username change (POST/PUT/PATCH /me/username/)."""

from unittest.mock import patch

from django.core.cache import cache
from django.db import IntegrityError
from rest_framework.test import APIClient

from apps.api.test_profile import PASSWORD, User, _ProfileBase
from apps.api.usernames import FORMAT_MESSAGE, UNAVAILABLE_MESSAGE


class ChangeUsernameTests(_ProfileBase):
    URL = '/api/v1/me/username/'

    def _change(self, client, username='alice.adams', password=PASSWORD, method='post'):
        return getattr(client, method)(
            self.URL, {'username': username, 'current_password': password}, format='json')

    def _fresh(self, client, username, **kw):
        """One attempt with a clean throttle bucket (the endpoint is rate-limited)."""
        cache.clear()
        return self._change(client, username, **kw)

    # -- the happy path --------------------------------------------------------
    def test_rename_normalises_and_signs_nobody_out(self):
        tokens = self._tokens().data
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}')

        resp = self._change(client, '  Alice.Adams ')            # mixed case + spaces
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data, {'username': 'alice.adams'})
        self.assertEqual(User.objects.get(pk=self.user_a.pk).username, 'alice.adams')

        # Tokens identify the user by id, so the same session keeps working...
        self.assertEqual(client.get('/api/v1/me/').data['username'], 'alice.adams')
        refreshed = APIClient().post('/api/v1/auth/refresh/',
                                     {'refresh': tokens['refresh']}, format='json')
        self.assertEqual(refreshed.status_code, 200)
        # ...and the new name is the login from now on; the old one is gone.
        self.assertEqual(self._tokens('alice.adams').status_code, 200)
        self.assertEqual(self._tokens('alice').status_code, 401)

    def test_me_exposes_the_current_username(self):
        self.assertEqual(self._login().get('/api/v1/me/').data['username'], 'alice')

    def test_put_and_patch_work_like_post(self):
        client = self._login()
        self.assertEqual(self._fresh(client, 'via.put', method='put').status_code, 200)
        self.assertEqual(self._fresh(client, 'via.patch', method='patch').status_code, 200)
        self.assertEqual(client.get('/api/v1/me/').data['username'], 'via.patch')

    def test_change_is_written_to_the_audit_log(self):
        with self.assertLogs('apps.api', 'INFO') as logs:
            self._change(self._login())
        self.assertIn("'alice' -> 'alice.adams'", ' '.join(logs.output))

    def test_may_go_back_to_their_own_employee_number(self):
        client = self._login()
        self._change(client, 'alice.adams')
        self.assertEqual(self._fresh(client, 'EMP-A').status_code, 200)   # own number is theirs
        self.assertEqual(User.objects.get(pk=self.user_a.pk).username, 'emp-a')

    # -- re-authentication -----------------------------------------------------------
    def test_wrong_password_is_rejected_and_nothing_changes(self):
        resp = self._change(self._login(), password='wrong-password')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('current_password', resp.data)
        self.assertEqual(User.objects.get(pk=self.user_a.pk).username, 'alice')

    def test_without_the_password_you_cannot_probe_which_names_exist(self):
        # A stolen token (no password) must learn nothing about existing usernames.
        client = self._login()
        taken = self._fresh(client, 'bob', password='wrong-password')
        free = self._fresh(client, 'nobody.uses.this', password='wrong-password')
        self.assertEqual(taken.status_code, 400)
        self.assertEqual(set(taken.data), {'current_password'})
        self.assertEqual(taken.data, free.data)

    # -- what a name may look like --------------------------------------------------
    def test_malformed_usernames_are_rejected(self):
        client = self._login()
        for bad in ('ab', 'a' * 31, 'has space', 'bad!char', '.leading', '-leading',
                    'ünïcode', 'semi;colon', ''):
            resp = self._fresh(client, bad)
            self.assertEqual(resp.status_code, 400, repr(bad))
            self.assertIn('username', resp.data, repr(bad))
        self.assertEqual(self._fresh(client, 'ab').data['username'], [FORMAT_MESSAGE])
        self.assertEqual(User.objects.get(pk=self.user_a.pk).username, 'alice')

    def test_boundary_lengths_are_accepted(self):
        client = self._login()
        self.assertEqual(self._fresh(client, 'abc').status_code, 200)
        self.assertEqual(self._fresh(client, 'a' * 30).status_code, 200)

    # -- what a name may not be -------------------------------------------------------
    def test_taken_reserved_and_lookalike_names_are_refused_with_one_message(self):
        client = self._login()
        for name in (
            'bob', 'BOB',                 # another user's login, any case
            'admin', 'root',              # reserved
            'ａｄｍｉｎ',                    # full-width "admin" must not sneak past the list
            'emp-b',                      # another employee's number
            'emp-9999',                   # employee-number style: reserved for future hires
        ):
            resp = self._fresh(client, name)
            self.assertEqual(resp.status_code, 400, name)
            self.assertEqual(resp.data['username'], [UNAVAILABLE_MESSAGE], name)
        self.assertEqual(User.objects.get(pk=self.user_a.pk).username, 'alice')

    def test_choosing_your_current_username_is_refused(self):
        resp = self._change(self._login(), 'ALICE')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['username'], ['This is already your username.'])

    def test_losing_a_race_for_a_name_is_a_clean_400(self):
        with patch.object(User, 'save', side_effect=IntegrityError):
            resp = self._change(self._login(), 'free.name')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['username'], [UNAVAILABLE_MESSAGE])

    # -- access control and throttling ------------------------------------------------
    def test_authentication_and_role_required(self):
        self.assertEqual(APIClient().post(self.URL, {}, format='json').status_code, 401)
        admin = User.objects.create(username='root2', role=User.Roles.ADMIN,
                                    is_staff=True, is_superuser=True)
        admin.set_password(PASSWORD)
        admin.save()
        self.assertEqual(self._change(self._login('root2')).status_code, 403)

    def test_shares_its_throttle_with_the_password_endpoint(self):
        # Both verify the current password, so together they get ONE 5/min budget.
        client = self._login()
        codes = [self._change(client, password='x').status_code for _ in range(3)]
        codes += [client.post('/api/v1/me/password/',
                              {'old_password': 'x', 'new_password': 'Whatever-123'},
                              format='json').status_code for _ in range(2)]
        codes.append(self._change(client, password='x').status_code)
        self.assertEqual(codes, [400] * 5 + [429])
