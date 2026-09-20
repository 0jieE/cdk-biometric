"""Shared helpers for the web portal: admin gate + HTMX detection."""

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


def is_htmx(request) -> bool:
    return request.headers.get('HX-Request') == 'true'


def admin_required(view):
    """Allow only authenticated ADMIN users. Employees are redirected away."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect('webportal:login')
        if not getattr(user, 'is_admin', False):
            messages.error(request, 'Admin access only.')
            return redirect('webportal:login')
        return view(request, *args, **kwargs)

    return wrapped
