"""The second step of signing in: every account proves a second factor.

A correct password (or a Civil SSO callback) no longer signs anyone in. It
parks the user's id in the session as *pending* and sends the browser to
``/login/verify/``, where one of three things finishes the sign-in:

  * a six-digit TOTP code,
  * a tap on an enrolled security key or passkey (the WebAuthn assertion is
    verified and its one-time token consumed in the same request), or
  * for an account with neither, enrolling TOTP right there — scan, confirm a
    code — before the dashboard opens. Nobody reaches it without a factor.

``auth.login`` is only ever called from here, after the factor checks out, so
a pending session is an anonymous session in every other view: the API, the
pages and the WebSockets all see no user.

Guesses count against the same per-user TOTP limit the confirmation prompts
use, so the second step cannot be brute-forced from the sign-in page either.
"""
from __future__ import annotations

import time

from django.contrib import auth
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import now
from django.views.decorators.http import require_POST
from webauthn.helpers.exceptions import InvalidAuthenticationResponse

from .models import UserProfile
from .totp import (
    _clear_totp_attempts,
    _totp_attempt_allowed,
    consume_totp,
    generate_secret,
    has_second_factor,
    otpauth_uri,
    verify_totp,
)
from .webauthn_keys import (
    authenticate as webauthn_authenticate,
    authentication_options,
    consume_key_token,
    relying_party,
    secure_context,
)

PENDING_KEY = "login_pending"
ENROL_KEY = "login_enrol_secret"
#: Long enough to find a phone or a key; short enough that a password typed on
#: a shared machine and walked away from does not stay half signed in.
PENDING_SECONDS = 300

_TOO_MANY = "Too many wrong codes. Wait a few minutes and try again."


def safe_next(request, candidate: str) -> str:
    """Same-host relative targets only; anything else is an open redirect."""
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return candidate
    return "/"


def begin(request, user, next_url: str = "/", backend: str = ""):
    """Park ``user`` as pending and send the browser to the second step."""
    request.session.pop(ENROL_KEY, None)
    request.session[PENDING_KEY] = {
        "uid": user.pk,
        "exp": time.time() + PENDING_SECONDS,
        "next": safe_next(request, next_url),
        "backend": backend or getattr(user, "backend", "")
        or "django.contrib.auth.backends.ModelBackend",
    }
    return redirect("login-verify")


def _pending(request):
    """(user, state) for a live pending sign-in, or (None, None)."""
    state = request.session.get(PENDING_KEY) or {}
    if not state or time.time() >= (state.get("exp") or 0):
        _forget(request)
        return None, None
    user = get_user_model().objects.filter(pk=state.get("uid"), is_active=True).first()
    if user is None:
        _forget(request)
        return None, None
    return user, state


def _forget(request) -> None:
    request.session.pop(PENDING_KEY, None)
    request.session.pop(ENROL_KEY, None)


def _finish(request, user, state) -> str:
    """Sign in for real. Returns where to go next."""
    _forget(request)
    auth.login(request, user, backend=state["backend"])
    return state.get("next") or "/"


def _keys_offered(request, user) -> bool:
    _rp_id, origin = relying_party(request)
    return secure_context(origin) and user.security_keys.exists()


def _enrol_context(request, user) -> dict:
    from .views import _totp_qr_svg

    secret = request.session.get(ENROL_KEY)
    if not secret:
        secret = generate_secret()
        request.session[ENROL_KEY] = secret
    uri = otpauth_uri(secret, user.get_username())
    return {"enrol": True, "totp_secret": secret, "totp_qr_svg": _totp_qr_svg(uri)}


def verify_view(request):
    user, state = _pending(request)
    if user is None:
        return redirect("login")

    enrol = not has_second_factor(user)
    error = None
    if request.method == "POST":
        code = request.POST.get("code", "").strip().replace(" ", "")
        if enrol:
            secret = request.session.get(ENROL_KEY) or ""
            if not _totp_attempt_allowed(user):
                error = _TOO_MANY
            elif secret and verify_totp(secret, code):
                _clear_totp_attempts(user)
                profile, _ = UserProfile.objects.get_or_create(user=user)
                profile.totp_secret = secret
                profile.totp_confirmed_at = now()
                # The code that confirmed enrolment is spent, as any other is.
                profile.last_totp_code = code
                profile.last_totp_used_at = now()
                profile.save()
                return redirect(_finish(request, user, state))
            else:
                error = "That code didn't match. Check your authenticator's clock and try again."
        elif code.startswith("wa_"):
            # Key tokens are handed over by the key endpoint below, never typed.
            error = "Enter the six-digit code from your authenticator."
        else:
            ok, err = consume_totp(user, code)
            if ok:
                return redirect(_finish(request, user, state))
            error = err

    context = {"error": error, "username": user.get_username()}
    if enrol:
        context.update(_enrol_context(request, user))
    else:
        context["keys"] = _keys_offered(request, user)
        context["has_totp"] = bool(getattr(user, "profile", None)
                                   and user.profile.totp_confirmed_at)
    return render(request, "login_verify.html", context, status=400 if error else 200)


@require_POST
def key_options_view(request):
    user, _state = _pending(request)
    if user is None:
        return JsonResponse({"error": "Your sign-in expired. Start again."}, status=401)
    try:
        return JsonResponse(authentication_options(request, user=user))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@require_POST
def key_view(request):
    import json

    user, state = _pending(request)
    if user is None:
        return JsonResponse({"error": "Your sign-in expired. Start again."}, status=401)
    if not _totp_attempt_allowed(user):
        return JsonResponse({"error": _TOO_MANY}, status=429)
    try:
        credential = json.loads(request.body or b"{}").get("credential")
        token = webauthn_authenticate(request, credential, user=user)
    except (ValueError, TypeError, AttributeError, InvalidAuthenticationResponse) as exc:
        return JsonResponse({"error": str(exc) or "That key could not be verified."}, status=400)
    if not consume_key_token(user, token):
        return JsonResponse({"error": "That key could not be verified."}, status=400)
    _clear_totp_attempts(user)
    return JsonResponse({"next": _finish(request, user, state)})
