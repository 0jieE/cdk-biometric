"""Token refresh that honours password changes.

With ``CHECK_REVOKE_TOKEN`` on, SimpleJWT embeds a hash of the user's password in
every token and rejects an *access* token whose hash no longer matches — but its
stock refresh endpoint never checks it. So a refresh token stolen before a
password change would still mint access tokens (each one dead on arrival, but
minted all the same, and the app would only learn on its next call).

This refresh view fails cleanly instead: a refresh token issued before the
password changed gets an immediate 401 ``password_changed``, telling the app to
send the user back to the login screen.
"""

from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.utils import get_md5_hash_password
from rest_framework_simplejwt.views import TokenRefreshView


class RevokeAwareTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        if api_settings.CHECK_REVOKE_TOKEN:
            refresh = self.token_class(attrs['refresh'])   # bad/expired -> 401 as usual
            user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
            user = (get_user_model().objects
                    .filter(**{api_settings.USER_ID_FIELD: user_id}).first()
                    if user_id else None)
            if user is not None and (
                    refresh.payload.get(api_settings.REVOKE_TOKEN_CLAIM)
                    != get_md5_hash_password(user.password)):
                # DRF renders only the message for a plain string, so pass a dict
                # to expose the machine-readable code the app keys off (same
                # {detail, code} shape SimpleJWT uses for its own auth errors).
                raise AuthenticationFailed(
                    {'detail': "The user's password has been changed.",
                     'code': 'password_changed'},
                    code='password_changed')
        return super().validate(attrs)


class RevokeAwareTokenRefreshView(TokenRefreshView):
    serializer_class = RevokeAwareTokenRefreshSerializer
