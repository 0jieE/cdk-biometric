"""Deliver Django messages to htmx requests as toasts.

A full page shows its messages through base.html, but an htmx action (save in
a modal, deactivate, ...) never renders base.html, so its "Saved." would only
surface on the next page load. For those responses the messages go out in an
HX-Trigger-After-Settle header instead; portal.js turns the ``toast`` event
into pop-ups.
"""

import json

from django.contrib.messages import get_messages


class HtmxMessagesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (request.headers.get('HX-Request') != 'true'
                or 300 <= response.status_code < 400       # htmx follows; next response gets them
                or 'HX-Trigger-After-Settle' in response):
            return response
        items = [{'level': m.level_tag or 'info', 'text': str(m)}
                 for m in get_messages(request)]            # iterating marks them shown
        if items:
            response['HX-Trigger-After-Settle'] = json.dumps({'toast': {'items': items}})
        return response
