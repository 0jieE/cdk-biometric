"""Rules for the username an employee may choose for themselves.

A username is a login credential, and the rest of the system derives usernames
from employee numbers (``EMP-1005`` -> ``emp-1005``) when it creates accounts, so
free-for-all renaming is unsafe: someone could take a colleague's future login, or
an admin-looking name. Everything here is about closing those doors.
"""

from __future__ import annotations

import re
import unicodedata

from apps.accounts.models import User
from apps.organization.models import Employee

# 3-30 chars; lowercase ASCII letters/digits, then also . _ -
USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{2,29}$')
FORMAT_MESSAGE = ('Use 3-30 characters: lowercase letters, digits, dots, hyphens '
                  'and underscores, starting with a letter or digit.')
# One message for every "you can't have that" reason, so it can't be used to probe
# which usernames / employee numbers exist.
UNAVAILABLE_MESSAGE = 'This username is not available.'

RESERVED = frozenset({
    'admin', 'administrator', 'root', 'superuser', 'staff', 'support', 'system',
    'api', 'portal', 'login', 'logout', 'me', 'null', 'none',
})
# The convention accounts are created with: an employee number, lowercased.
_NUMBER_STYLE = re.compile(r'^emp-\d+$')


def normalize_username(raw: str) -> str:
    """NFKC-fold (full-width letters -> ASCII, so 'ａｄｍｉｎ' can't slip past the
    reserved list), trim and lowercase. MySQL compares logins case-insensitively
    anyway, so two spellings of one name would otherwise be the same account."""
    return unicodedata.normalize('NFKC', raw).strip().lower()


def format_problem(candidate: str) -> str | None:
    """A message if `candidate` (already normalised) is malformed, else None."""
    return None if USERNAME_RE.match(candidate) else FORMAT_MESSAGE


def availability_problem(candidate: str, user: User) -> str | None:
    """A message if this user can't have `candidate`, else None. Needs the DB, so
    callers should only run it after re-authenticating the user."""
    if candidate == user.username.lower():
        return 'This is already your username.'
    if candidate in RESERVED:
        return UNAVAILABLE_MESSAGE
    if User.objects.filter(username__iexact=candidate).exclude(pk=user.pk).exists():
        return UNAVAILABLE_MESSAGE
    # Employee-number style logins belong to that employee (or to a future hire).
    own_number = user.employee.employee_no.lower() if user.employee_id else ''
    if candidate != own_number and (
            _NUMBER_STYLE.match(candidate)
            or Employee.objects.filter(employee_no__iexact=candidate).exists()):
        return UNAVAILABLE_MESSAGE
    return None
