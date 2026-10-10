"""Sign-in sessions end after a stretch of idleness, and after a hard cap (QA-14).

Only real use counts as activity: a page navigation, a change (any non-GET),
or the browser's heartbeat, which vigil-session.js sends when someone actually
touches the page. Background polling is checked but never refreshes the clock —
otherwise an open Monitor tab would keep a session alive forever.
"""

import time
from urllib.parse import urlencode

from django.contrib.auth import logout
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

LOGIN_AT = "vigil_login_at"
LAST_ACTIVE = "vigil_last_active"
HEARTBEAT_PATH = "/api/v1/accounts/session/activity/"
_SAFE = ("GET", "HEAD", "OPTIONS")
_ASSET_PREFIXES = ("/static/", "/media/")


def limits() -> tuple[int, int]:
    """(idle seconds, max seconds) — the effective instance settings."""
    from apps.instance.config import setting

    idle = int(setting("VIGIL_SESSION_IDLE_MINUTES") or 15)
    cap = int(setting("VIGIL_SESSION_MAX_HOURS") or 12)
    return max(idle, 5) * 60, max(cap, 1) * 3600


def is_activity(request) -> bool:
    path = request.path
    if path == HEARTBEAT_PATH:
        return True
    if request.method not in _SAFE:
        return True
    return not path.startswith("/api/") and not path.startswith(_ASSET_PREFIXES)


def remaining(session, now=None) -> dict:
    """Seconds left before each limit ends the session (never negative)."""
    now = now if now is not None else time.time()
    idle, cap = limits()
    last = session.get(LAST_ACTIVE, now)
    start = session.get(LOGIN_AT, now)
    return {
        "idle_seconds_left": max(0, int(last + idle - now)),
        "max_seconds_left": max(0, int(start + cap - now)),
        "idle_seconds": idle,
        "max_seconds": cap,
    }


@receiver(user_logged_in)
def _stamp_login(sender, request, user, **kwargs):
    now = time.time()
    request.session[LOGIN_AT] = now
    request.session[LAST_ACTIVE] = now


class SessionTimeoutMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and hasattr(request, "session")
            and not request.path.startswith(_ASSET_PREFIXES)
        ):
            now = time.time()
            session = request.session
            # A session from before this feature has no stamps: start its clocks now
            # rather than signing everyone out on upgrade.
            session.setdefault(LOGIN_AT, now)
            session.setdefault(LAST_ACTIVE, now)
            left = remaining(session, now)
            reason = (
                "max"
                if left["max_seconds_left"] <= 0
                else "idle"
                if left["idle_seconds_left"] <= 0
                else ""
            )
            if reason:
                logout(request)
                return self._signed_out(request, reason)
            if is_activity(request):
                session[LAST_ACTIVE] = now
        return self.get_response(request)

    @staticmethod
    def _signed_out(request, reason):
        message = (
            "Signed out after a period of inactivity."
            if reason == "idle"
            else "Signed out — sign-ins last at most a set number of hours."
        )
        if request.path.startswith("/api/"):
            return JsonResponse(
                {"detail": message, "reason": "session_" + reason}, status=401
            )
        query = {"expired": reason}
        if request.method == "GET":
            query["next"] = request.get_full_path()
        return redirect(f"{reverse('login')}?{urlencode(query)}")
