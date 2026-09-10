"""The two-legged Civil SSO flow: start → Civil → callback.

Failure philosophy: every failure path lands on the ordinary login page
(with ``?civil=failed`` so the template can show one quiet line). Civil
being down, misconfigured, or rejecting a token must never produce an
error page or lock out local login.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model, login
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from apps.civilsso import client
from apps.civilsso.models import CivilIdentity

logger = logging.getLogger("civilsso")

_STATE_SESSION_KEY = "civilsso_state"
_NEXT_SESSION_KEY = "civilsso_next"


def login_start(request):
    """Kick off SSO: remember state + next, bounce to Civil's authorize."""
    if not client.enabled():
        raise Http404  # feature is opt-in; unconfigured = this URL doesn't exist
    state = secrets.token_urlsafe(24)
    request.session[_STATE_SESSION_KEY] = state
    nxt = request.GET.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        request.session[_NEXT_SESSION_KEY] = nxt
    query = urlencode({
        "app": client.app_slug(),
        "redirect_uri": request.build_absolute_uri(reverse("civil-callback")),
        "state": state,
    })
    return redirect(f"{client.civil_url()}/sso/authorize?{query}")


def callback(request):
    """Verify the handoff token, map/provision the local user, log in."""
    if not client.enabled():
        raise Http404

    def fail(reason: str):
        logger.warning("Civil SSO failed: %s", reason)
        return redirect("/login/?civil=failed")

    state = request.session.pop(_STATE_SESSION_KEY, None)
    if not state or state != request.GET.get("state", ""):
        return fail("state mismatch")
    claims = client.verify_sso_token(request.GET.get("token", ""))
    if claims is None:
        return fail("token rejected")

    civil_id = claims["sub"]
    identity = CivilIdentity.objects.filter(civil_id=civil_id).select_related("user").first()
    if identity is None:
        user = _provision_user(claims)
        identity = CivilIdentity.objects.create(user=user, civil_id=civil_id)
    user = identity.user
    if not user.is_active:
        return fail(f"local user {user.pk} is inactive")

    login(request, user)
    nxt = request.session.pop(_NEXT_SESSION_KEY, "") or "/"
    return redirect(nxt)


def _provision_user(claims: dict):
    """First Civil login for this human on this app: create a local user.

    Username prefers Civil's, dodging collisions with a short suffix —
    an existing local "alice" is a DIFFERENT account than Civil's alice
    unless an admin links them by creating the CivilIdentity row manually.
    Auto-claiming a matching local username would let a Civil admin
    impersonate a pre-existing local account; that stays a human decision.
    """
    User = get_user_model()
    base = (claims.get("preferred_username") or f"civil-{claims['sub'][:8]}")[:140]
    username = base
    n = 2
    while User.objects.filter(username=username).exists():
        username = f"{base}-{n}"
        n += 1
    user = User.objects.create_user(
        username=username,
        email=claims.get("email", "") or "",
    )
    user.set_unusable_password()  # Civil is this account's only way in
    name = (claims.get("name") or "").strip()
    if name:
        user.first_name = name.split(" ")[0][:150]
        user.last_name = " ".join(name.split(" ")[1:])[:150]
    user.save()
    logger.info("provisioned local user %s for civil:%s", username, claims["sub"])
    return user


def _is_safe_civil_url(url: str) -> bool:
    """True when *url* is safe to fetch Civil's signing key from.

    HTTPS anywhere; plain HTTP only on a private network. Vigil is a homelab
    product and a Civil on the LAN reached over a switch the operator owns is
    an ordinary deployment, not a mistake — the same judgement the rebuild
    ceremony makes about its own transport. Plain HTTP to a *public* address is
    the case that matters: the key fetched over it authenticates every SSO
    login, and anyone on the path can replace it.
    """
    from urllib.parse import urlsplit

    from vigil.netutils import is_private_hostname

    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    if parts.scheme == "http":
        return is_private_hostname(parts.hostname or "")
    return False


def civil_settings_api(request):
    """GET/POST the Civil connection config. Admin-only, CSRF-protected by
    the session middleware; framework-light so the same file ports across
    the SQSY family."""
    import json as _json

    from django.http import JsonResponse

    from .models import CachedCivilKey, CivilConfig

    # role_of, not is_staff. They disagree: role_of consults per-site role rows
    # first and, for a user who has any, never looks at is_staff at all — so a
    # site-scoped Viewer carrying is_staff was refused by IsAdmin everywhere in
    # Vigil and accepted right here, on the endpoint that can replace the key
    # every SSO login is verified against.
    from apps.accounts.permissions import OWNER, Role, role_of

    user = getattr(request, "user", None)
    if not (user and user.is_authenticated
            and role_of(user) in (Role.ADMIN, OWNER)):
        return JsonResponse({"detail": "Administrator access required."}, status=403)

    cfg = CivilConfig.current()
    data = {}
    if request.method == "POST":
        try:
            data = _json.loads(request.body.decode() or "{}")
        except ValueError:
            return JsonResponse({"detail": "invalid JSON"}, status=400)
        if "url" in data:
            new_url = (data["url"] or "").strip().rstrip("/")
            # Refuse plain HTTP. This URL is where the public key that
            # authenticates every SSO login is fetched from, over a bare
            # urlopen with no pinning — one MITM of that single request
            # substitutes the key and mints tokens for any account. A
            # loopback URL stays allowed so a developer can run Civil locally.
            if new_url and not _is_safe_civil_url(new_url):
                return JsonResponse(
                    {"detail": "A public Civil URL must be https://. The "
                               "public key that verifies every SSO login is "
                               "fetched from it with no pinning, so plain HTTP "
                               "over the internet lets anyone on the path "
                               "replace that key. Plain HTTP to a LAN address "
                               "is still accepted."},
                    status=400)
            cfg.url = new_url
        if "app_slug" in data:
            cfg.app_slug = (data["app_slug"] or "").strip()
        if "enabled" in data:
            cfg.enabled = bool(data["enabled"])
        cfg.save()

    payload = {
        "enabled": cfg.enabled,
        "url": cfg.url,
        "app_slug": cfg.app_slug or client.app_slug(),
        "effective_url": client.civil_url(),
        "env_override": bool(getattr(dj_settings, "CIVIL_URL", "")),
        "active": client.enabled(),
        "key_cached": bool(CachedCivilKey.current()),
    }
    if data.get("test") or data.get("refresh_key"):
        pem = client.get_public_key(force_fetch=True) if client.enabled() else ""
        payload["test_ok"] = bool(pem)
        payload["key_cached"] = bool(CachedCivilKey.current())
    return JsonResponse(payload)
