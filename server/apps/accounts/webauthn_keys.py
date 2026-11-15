"""WebAuthn / FIDO2 security keys and passkeys (QA-65).

Options, ceremony verification, and the one-time key token that lets a key tap
stand in for a six-digit TOTP code at every existing confirmation gate.

Shape of the exchange:

  register:  GET options  → session["webauthn_reg"] = {challenge, exp}
             POST credential → verify against that challenge → SecurityKey row
             (the rp id is added to the creation JSON as ``rpId``, which it does
             not carry on its own, and the origin as ``origin``)

  assert:    GET options  → session["webauthn_auth"] = {challenge, exp}
             POST credential → verify → wa_… token cached against the user id

  sign in:   POST options → session["webauthn_login"] = {challenge, exp}, no
             credentials listed, so the browser offers any passkey it holds
             POST credential → find the key by its id → verify, user
             verification required → that key's owner (``passwordless_user``)

`consume_totp` recognises the ``wa_`` prefix and hands it to
`consume_key_token`, which is single-use by construction: the token is only
accepted by the request that manages to `cache.delete` it.
"""
from __future__ import annotations

import json
import secrets
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.utils.timezone import now
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

#: A key token is a stand-in for a TOTP code, so it lives about as long as one.
KEY_TOKEN_PREFIX = "wa_"
KEY_TOKEN_SECONDS = 60
#: Long enough for a slow authenticator or a phone that has to wake up.
CHALLENGE_SECONDS = 120

RP_NAME = "Vigil"


def _required_party(request) -> tuple[str, str]:
    """The relying party, or a ValueError saying why keys are unavailable."""
    rp_id, origin = relying_party(request)
    reason = unavailable_reason(origin)
    if reason:
        raise ValueError(reason)
    return rp_id, origin


def relying_party(request) -> tuple[str, str]:
    """(rp id, origin) — the two values the browser and we must agree on.

    Only ever ``VIGIL_PUBLIC_URL``: the identity a key is bound to comes from
    configuration, never from a request header. Without it this returns
    ``("", "")`` and security keys are unavailable (see ``unavailable_reason``).
    ``request`` is accepted for the callers' convenience and not read.
    """
    public = (getattr(settings, "VIGIL_PUBLIC_URL", "") or "").strip().rstrip("/")
    if not public:
        return "", ""
    split = urlsplit(public if "//" in public else f"https://{public}")
    host = split.hostname or ""
    scheme = split.scheme or "https"
    port = f":{split.port}" if split.port else ""
    return host, f"{scheme}://{host}{port}"


def secure_context(origin: str) -> bool:
    """WebAuthn only runs where the browser will run it: https, or localhost."""
    if not origin:
        return False
    split = urlsplit(origin)
    host = split.hostname or ""
    return split.scheme == "https" or host in ("localhost", "127.0.0.1")


def unavailable_reason(origin: str) -> str:
    """Why security keys cannot be used here, or "" when they can."""
    if not origin:
        return ("Set VIGIL_PUBLIC_URL to the address you open Vigil at to use "
                "security keys.")
    if not secure_context(origin):
        return "Security keys need HTTPS."
    return ""


def _store_challenge(session, key: str, challenge: bytes) -> None:
    session[key] = {"challenge": bytes_to_base64url(challenge),
                    "exp": time.time() + CHALLENGE_SECONDS}


def _take_challenge(session, key: str) -> bytes:
    """The stored challenge, once. Reading it removes it."""
    pending = session.get(key) or {}
    session.pop(key, None)
    challenge = pending.get("challenge") or ""
    exp = pending.get("exp") or 0
    if not challenge or time.time() >= exp:
        raise ValueError("The challenge has expired. Start again.")
    return base64url_to_bytes(challenge)


def registration_options(request) -> dict:
    """Creation options, with the user's existing keys excluded so an already
    enrolled authenticator cannot silently be enrolled twice.

    The rp id is in the JSON (``rpId``) but the origin is not — a browser takes
    it from where the page was served, so we return ours as ``origin`` for the
    dialog to read out of the options rather than hardcode its own guess.
    """
    from .models import SecurityKey

    rp_id, origin = _required_party(request)
    # The same bytes go to the browser and into the session: verification
    # compares what the authenticator signed against exactly this.
    challenge = secrets.token_bytes(32)
    _store_challenge(request.session, "webauthn_reg", challenge)
    options = generate_registration_options(
        challenge=challenge,
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_name=request.user.get_username(),
        user_id=str(request.user.id).encode(),
        user_display_name=request.user.get_username(),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(key.credential_id))
            for key in SecurityKey.objects.filter(user=request.user)
        ],
    )
    result = json.loads(options_to_json(options))
    result["rpId"] = rp_id
    result["origin"] = origin
    return result


def _clean_name(raw) -> str:
    name = (raw or "").strip() if isinstance(raw, str) else ""
    if not 1 <= len(name) <= 60:
        raise ValueError("Give the key a name of 1 to 60 characters.")
    return name


def _credential_id(credential) -> bytes:
    """The authenticator's raw credential id, however the client spelled it.

    ``rawId`` is what the WebAuthn spec puts on the response object; the JSON
    we build carries both, and a hand-made credential may carry only one.
    """
    raw = credential.get("id") or credential.get("rawId")
    if not raw:
        raise ValueError("The credential is missing its id.")
    return base64url_to_bytes(raw)


def register(request, name, credential):
    """Verify a registration response against the session challenge and save."""
    from .models import SecurityKey

    rp_id, origin = _required_party(request)
    expected = _take_challenge(request.session, "webauthn_reg")
    verified = verify_registration_response(
        credential=credential,
        expected_challenge=expected,
        expected_rp_id=rp_id,
        expected_origin=origin,
    )
    return SecurityKey.objects.create(
        user=request.user,
        name=_clean_name(name),
        credential_id=bytes(verified.credential_id),
        public_key=bytes(verified.credential_public_key),
        sign_count=verified.sign_count,
        aaguid=verified.aaguid or "",
    )


def authentication_options(request, user=None) -> dict:
    """Request options listing this user's keys, so the browser can offer
    exactly the authenticators that can satisfy them.

    ``user`` is the signed-in user unless given: the sign-in page passes the
    account whose password was just accepted, before anyone is signed in.
    """
    from .models import SecurityKey

    user = user or request.user
    keys = list(SecurityKey.objects.filter(user=user))
    if not keys:
        raise ValueError("No security keys enrolled")
    rp_id, _origin = _required_party(request)
    challenge = secrets.token_bytes(32)
    _store_challenge(request.session, "webauthn_auth", challenge)
    options = generate_authentication_options(
        challenge=challenge,
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(key.credential_id)) for key in keys
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return json.loads(options_to_json(options))


def authenticate(request, credential, user=None) -> str:
    """Verify an assertion and issue a one-time key token for it.

    The credential is looked up among *this* user's keys only: a token must
    never be issuable by presenting somebody else's authenticator.
    """
    from .models import SecurityKey

    user = user or request.user
    rp_id, origin = _required_party(request)
    expected = _take_challenge(request.session, "webauthn_auth")
    raw_id = _credential_id(credential)
    key = SecurityKey.objects.filter(user=user, credential_id=raw_id).first()
    if key is None:
        raise ValueError("That security key is not enrolled for this account.")
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=expected,
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=bytes(key.public_key),
        credential_current_sign_count=key.sign_count,
    )
    key.sign_count = verified.new_sign_count
    key.last_used_at = now()
    key.save(update_fields=["sign_count", "last_used_at"])

    token = KEY_TOKEN_PREFIX + secrets.token_urlsafe(32)
    cache.set(f"webauthn:token:{token}", user.id, KEY_TOKEN_SECONDS)
    return token


def consume_key_token(user, token: str) -> bool:
    """True for exactly one caller. ``cache.delete`` returns True only for the
    request that actually removed the entry, so two requests racing on one
    token cannot both pass a gate with it."""
    if not isinstance(token, str) or not token.startswith(KEY_TOKEN_PREFIX):
        return False
    owner = cache.get(f"webauthn:token:{token}")
    if owner != user.id:
        return False
    return bool(cache.delete(f"webauthn:token:{token}"))


#: The one answer every refused passkey sign-in gets: which part failed (no
#: such key, a disabled account, a bad signature) is not the caller's business.
PASSKEY_REFUSED = "That passkey could not sign you in."


def passwordless_options(request) -> dict:
    """Request options for signing in with a passkey alone.

    No credentials are listed: nobody has said who they are yet, so the browser
    offers whichever discoverable credentials it holds for this rp id. User
    verification is required, because the passkey replaces both the password
    and the second step: the PIN or biometric is the factor the password was.
    """
    rp_id, _origin = _required_party(request)
    challenge = secrets.token_bytes(32)
    _store_challenge(request.session, "webauthn_login", challenge)
    options = generate_authentication_options(
        challenge=challenge,
        rp_id=rp_id,
        allow_credentials=[],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return json.loads(options_to_json(options))


def passwordless_user(request, credential):
    """Verify a passkey sign-in and return the account it belongs to.

    Raises ``ValueError(PASSKEY_REFUSED)`` for anything short of a verified,
    user-verified assertion from a key enrolled to an active account. The
    ``userHandle`` must name the key's owner: a discoverable credential always
    returns it, so a missing one (read as empty) is refused too.
    """
    from .models import SecurityKey

    rp_id, origin = _required_party(request)
    expected = _take_challenge(request.session, "webauthn_login")
    if not isinstance(credential, dict):
        raise ValueError(PASSKEY_REFUSED)
    key = (SecurityKey.objects.select_related("user")
           .filter(credential_id=_credential_id(credential)).first())
    handle = (credential.get("response") or {}).get("userHandle") or ""
    if (key is None or not key.user.is_active
            or base64url_to_bytes(handle) != str(key.user_id).encode()):
        raise ValueError(PASSKEY_REFUSED)
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=bytes(key.public_key),
            credential_current_sign_count=key.sign_count,
            require_user_verification=True,
        )
    except (InvalidAuthenticationResponse, ValueError, TypeError, KeyError) as exc:
        # The library's own refusal, or a credential too malformed to parse.
        raise ValueError(PASSKEY_REFUSED) from exc
    key.sign_count = verified.new_sign_count
    key.last_used_at = now()
    key.save(update_fields=["sign_count", "last_used_at"])
    return key.user
