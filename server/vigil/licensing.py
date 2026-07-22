"""License verification, instance binding, and feature gating.

The governing spec is ``SQSY-LICENSING.md`` (SQSY planning repo). The rules
that shape every line here (§0, §6):

- **Nothing blocks. Ever.** Licensing failures degrade to the free tier with
  a banner. Any code path where a licensing concern could break monitoring is
  a severity-one bug. ``current_state()`` cannot raise.
- **Gates are bookkeeping, not enforcement.** No obfuscation, no phone-home,
  no kill switch. Verification is local and offline against a baked-in
  public key.
- **Instance binding** stops casual key sharing: a license carries the
  instance UUID it was issued for, and this deployment ignores licenses
  issued for any other instance. That's the whole anti-sharing story — no
  hardware fingerprinting (breaks container reschedules).

Wire format and signing live in the Mercantil repo (``sqsy_license``
package) — the ~40 lines of verification are vendored here so the AGPL core
has zero dependency on the commercial tooling. If the format ever changes,
change it there first; this file follows.

Free features are hardcoded ON and never consult the license. Business
features require a currently-effective license that includes them. The
public API surface is:

- ``has_feature(name)`` — the one gate. Template/vew code never inspects
  claims directly.
- ``current_state()`` — cached state for the license screen and banners.
- ``set_license(blob)`` / ``reload()`` — UI paste path; no restart needed.
- ``require_feature(name)`` — DRF permission for Business API endpoints.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from enum import Enum

from django.conf import settings

logger = logging.getLogger("vigil.licensing")

PREFIX = "SQSY-LICENSE-V1"
GRACE_DAYS = 14

#: Always on, for everyone, forever. Never consult the license for these.
#: Monitoring/alerting/agents are deliberately NOT feature names — they are
#: not features, they are the product, and no code path may gate them.
FREE_FEATURES = frozenset({
    "baselines",
    "ai_suggestions",   # BYO endpoint — same code path Business runs (§2)
    "status_pages",     # the basic page; branding/custom-domain is Business
    "jackil_integration",
})

#: Business features a license can grant. Keep in sync with
#: ``sqsy_license.BUSINESS_FEATURES`` in the Mercantil repo.
BUSINESS_FEATURES = frozenset({
    "sites",
    "audit_log",
    "rbac_advanced",    # OPERATOR + custom roles; Free has ADMIN + VIEWER
    "branding",
    "status_branding",  # branded/public/custom-domain status pages
    "sso",
})

#: Free-tier limits (soft — exceeded means a banner, never a block).
FREE_SEATS = 2   # 1 admin + 1 read-only
FREE_SITES = 1


class Status(str, Enum):
    NONE = "none"            # no license at all → free tier
    VALID = "valid"          # verifies, bound to us, unexpired
    GRACE = "grace"          # expired ≤14d ago — Business stays ON, loud banner
    LAPSED = "lapsed"        # expired >14d — Business off, monitoring untouched
    MISMATCH = "mismatch"    # verifies but bound to a different instance → free
    INVALID = "invalid"      # present but does not verify → free


@dataclass(frozen=True)
class Claims:
    instance: str
    org: str
    seats: int
    exp: int
    iat: int
    sites: int | None = None
    features: tuple[str, ...] = field(default=tuple(sorted(BUSINESS_FEATURES)))


@dataclass(frozen=True)
class LicenseState:
    status: Status
    claims: Claims | None = None
    detail: str = ""          # human-readable reason for INVALID/MISMATCH
    source: str = ""          # "env" | "db" | ""

    @property
    def business_active(self) -> bool:
        """Business features on? True while VALID or in the grace window."""
        return self.status in (Status.VALID, Status.GRACE)

    @property
    def tier(self) -> str:
        return "business" if self.business_active else "free"


class _VerifyError(Exception):
    pass


def _unb64u(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad)
    except (binascii.Error, ValueError) as exc:
        raise _VerifyError(f"not valid base64url: {exc}") from exc


def _verify_blob(blob: str, public_key_b64: str) -> Claims:
    """Vendored sqsy_license.verify — Ed25519 over the exact payload bytes.

    Expiry is deliberately NOT checked here: the §6 degradation ladder needs
    the claims of an expired-but-genuine license (grace period, "expired N
    days ago" banner), so expiry is policy in :func:`_classify`, not
    validity here.
    """
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    if not public_key_b64:
        raise _VerifyError("no license public key configured in this build")
    parts = blob.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise _VerifyError("not a SQSY-LICENSE-V1 blob")
    payload, sig = _unb64u(parts[1]), _unb64u(parts[2])
    try:
        VerifyKey(base64.b64decode(public_key_b64, validate=True)).verify(payload, sig)
    except (BadSignatureError, binascii.Error, ValueError) as exc:
        raise _VerifyError(f"signature verification failed: {exc}") from exc
    try:
        d = json.loads(payload.decode())
        features = d.get("features")
        return Claims(
            instance=str(d["instance"]),
            org=str(d["org"]),
            seats=int(d["seats"]),
            exp=int(d["exp"]),
            iat=int(d["iat"]),
            sites=None if d.get("sites") is None else int(d["sites"]),
            features=tuple(sorted(BUSINESS_FEATURES)) if features is None
            else tuple(features),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError,
            json.JSONDecodeError) as exc:
        raise _VerifyError(f"license claims malformed: {exc}") from exc


# --------------------------------------------------------------------------
# Instance identity

def instance_id() -> str:
    """This deployment's UUID — generated at first use, stored in the DB.

    Surfaced on the license screen, in ``GET /api/v1/license/``, and in the
    startup log so support can ask for it. If the DB is unreachable we return
    a sentinel rather than raising — licensing must never take anything down.
    """
    try:
        from apps.licensing.models import InstanceIdentity
        return str(InstanceIdentity.get().id)
    except Exception:  # noqa: BLE001 — deliberately never raises (§0)
        logger.exception("could not read instance identity; treating license as absent")
        return "unavailable"


# --------------------------------------------------------------------------
# License loading + classification

def load_blob() -> tuple[str, str]:
    """Return ``(blob, source)`` by spec §4 priority: env var, then DB paste.

    The documented ``--license-key`` CLI path is ``manage.py license set``,
    which writes the DB row — a flag on gunicorn isn't a real thing.
    """
    env = os.environ.get("VIGIL_LICENSE_KEY", "").strip()
    if env:
        return env, "env"
    try:
        from apps.licensing.models import StoredLicense
        blob = StoredLicense.current_blob()
        return (blob, "db") if blob else ("", "")
    except Exception:  # noqa: BLE001
        logger.exception("could not read stored license; treating as absent")
        return "", ""


def _classify(blob: str, source: str, *, now: int | None = None) -> LicenseState:
    if not blob:
        return LicenseState(Status.NONE)
    now = int(time.time()) if now is None else now
    try:
        claims = _verify_blob(blob, getattr(settings, "VIGIL_LICENSE_PUBLIC_KEY", ""))
    except _VerifyError as exc:
        logger.warning("license present but invalid: %s", exc)
        return LicenseState(Status.INVALID, detail=str(exc), source=source)
    my_id = instance_id()
    if claims.instance != my_id:
        # §6: treated as NO license — free tier, monitoring untouched.
        return LicenseState(
            Status.MISMATCH, claims=claims, source=source,
            detail=(f"license issued for instance {claims.instance}; "
                    f"this deployment is {my_id}"),
        )
    if now < claims.exp:
        return LicenseState(Status.VALID, claims=claims, source=source)
    if now <= claims.exp + GRACE_DAYS * 86400:
        return LicenseState(Status.GRACE, claims=claims, source=source)
    return LicenseState(Status.LAPSED, claims=claims, source=source)


# --------------------------------------------------------------------------
# Cached state — reloaded on save, no restart required (§4)

_lock = threading.Lock()
_cached: LicenseState | None = None
_cached_at: float = 0.0
#: Recheck wall-clock expiry this often even without an explicit reload, so a
#: long-running worker notices T-day transitions without a restart.
_TTL_SECONDS = 300


def current_state() -> LicenseState:
    """The current license state. Cached; cannot raise (§0)."""
    global _cached, _cached_at
    with _lock:
        if _cached is not None and (time.time() - _cached_at) < _TTL_SECONDS:
            return _cached
    try:
        state = _classify(*load_blob())
    except Exception:  # noqa: BLE001 — belt and braces; §0
        logger.exception("license classification failed; degrading to free tier")
        state = LicenseState(Status.INVALID, detail="internal error (see logs)")
    with _lock:
        _cached, _cached_at = state, time.time()
    return state


def reload() -> LicenseState:
    """Drop the cache and re-read (used after a paste and by tests)."""
    global _cached
    with _lock:
        _cached = None
    return current_state()


def set_license(blob: str) -> LicenseState:
    """UI/management-command paste path: store in DB, reload immediately."""
    from apps.licensing.models import StoredLicense
    StoredLicense.replace(blob)
    state = reload()
    logger.info("license updated via %s → %s", state.source or "paste",
                state.status.value)
    return state


# --------------------------------------------------------------------------
# The gate

def has_feature(name: str) -> bool:
    """The one feature gate. Free features are always on; Business features
    need a currently-effective license that grants them."""
    if name in FREE_FEATURES:
        return True
    state = current_state()
    return bool(
        state.business_active
        and state.claims is not None
        and name in state.claims.features
    )


class PaymentRequired(Exception):
    """Raised by require_feature gates; DRF renders it as HTTP 402."""

    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("detail", "license required"))


def upgrade_body(name: str) -> dict:
    return {
        "feature": name,
        "licensed": False,
        "detail": f"'{name}' requires a Vigil Business license.",
        "upgrade_url": "https://susquehannasyntax.com/subscribe",
    }


def require_feature(name: str):
    """DRF permission class factory for Business API endpoints.

    Unlicensed calls get 402 with a structured body — distinct from authz
    403s so the frontend renders an upgrade prompt instead of an error. UI
    panels should already be greyed from ``GET /api/v1/license/``; this is
    the backstop, not the UX (§5: upgrade prompts, not 403s).
    """
    from rest_framework.exceptions import APIException
    from rest_framework.permissions import BasePermission

    class _PaymentRequired(APIException):
        status_code = 402
        default_detail = upgrade_body(name)
        default_code = "license_required"

    class _HasLicensedFeature(BasePermission):
        message = upgrade_body(name)

        def has_permission(self, request, view):
            if has_feature(name):
                return True
            raise _PaymentRequired()

    _HasLicensedFeature.__name__ = f"Requires_{name}"
    return _HasLicensedFeature


# --------------------------------------------------------------------------
# Seats (§6: whatever holds the users counts them; Vigil reads its own view)

def seats_used() -> int:
    try:
        from django.contrib.auth import get_user_model
        return get_user_model().objects.filter(is_active=True).count()
    except Exception:  # noqa: BLE001
        logger.exception("seat count unavailable")
        return 0


def seats_allowed() -> int:
    state = current_state()
    if state.business_active and state.claims:
        return state.claims.seats
    return FREE_SEATS


# --------------------------------------------------------------------------
# Banners — the §6 ladder. Pure data; templates render it.

def banners(*, now: int | None = None) -> list[dict]:
    """Every licensing banner currently due, worst first.

    Each: ``{"severity": "info"|"warning"|"critical", "message": str}``.
    Banners are the entire enforcement mechanism — there is nothing else.
    """
    now = int(time.time()) if now is None else now
    state = current_state()
    out: list[dict] = []

    if state.status is Status.INVALID:
        out.append({"severity": "warning", "message":
                    f"A license is configured but does not verify ({state.detail}). "
                    "Running as Free — monitoring is unaffected."})
    elif state.status is Status.MISMATCH:
        out.append({"severity": "warning", "message":
                    f"{state.detail}. Re-bind it at susquehannasyntax.com. "
                    "Running as Free — monitoring is unaffected."})
    elif state.status is Status.GRACE:
        days_over = (now - state.claims.exp) // 86400
        out.append({"severity": "critical", "message":
                    f"Business license expired {days_over}d ago — features stay on "
                    f"for {GRACE_DAYS - days_over} more day(s). Renew at "
                    "susquehannasyntax.com."})
    elif state.status is Status.LAPSED:
        out.append({"severity": "warning", "message":
                    "Business license expired; Business features are off. "
                    "Monitoring, alerting, and agents are unaffected — forever."})
    elif state.status is Status.VALID:
        days_left = (state.claims.exp - now) // 86400
        if days_left <= 7:
            out.append({"severity": "critical",
                        "message": f"Business license expires in {days_left}d."})
        elif days_left <= 14:
            out.append({"severity": "warning",
                        "message": f"Business license expires in {days_left}d."})
        elif days_left <= 30:
            out.append({"severity": "info",
                        "message": f"Business license expires in {days_left}d."})

    used, allowed = seats_used(), seats_allowed()
    if used > allowed:
        # Seat #N over the licensed count WORKS (§6) — this is the whole response.
        target = ("update your subscription at susquehannasyntax.com"
                  if state.business_active else "Vigil Business")
        out.append({"severity": "info", "message":
                    f"{used}/{allowed} seats in use — {target}."})
    return out
