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
