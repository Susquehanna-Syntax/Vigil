"""Per-request ids, carried on every log line the request produces.

An operator following one request through the logs needs an id that is
present from the first log statement to the last, including everything the
framework logs before a view runs. A ``contextvars`` variable set by the
outermost middleware gives that without threading an id through call
signatures.
"""

import contextvars
import logging
import re
import uuid

_current = contextvars.ContextVar("vigil_request_id", default="-")
_SAFE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
HEADER = "X-Request-ID"


def current() -> str:
    """The request id for the request being handled, or ``"-"`` outside one."""
    return _current.get()


def _choose(incoming):
    """Accept a trustworthy inbound id, otherwise invent one.

    A header that fails the pattern is replaced, never echoed: an attacker
    who can put arbitrary text into ``X-Request-ID`` would otherwise write
    into every log line the request produces — forged entries, ANSI escapes,
    whatever their client sends.
    """
    if isinstance(incoming, str) and _SAFE.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Bind an id to the request, expose it on the response and the logs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = _choose(request.META.get("HTTP_X_REQUEST_ID"))
        request.request_id = rid
        token = _current.set(rid)
        try:
            response = self.get_response(request)
        finally:
            # Reset before the response leaves: a worker thread that kept the
            # id would stamp the next, unrelated request with it.
            _current.reset(token)
        response[HEADER] = rid
        return response


class RequestIdFilter(logging.Filter):
    """Stamp every record with the id of the request that logged it."""

    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = current()
        return True
