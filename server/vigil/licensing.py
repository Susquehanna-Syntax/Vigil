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
    "dashboards",
    "playbooks",
    "ai_suggestions",   # BYO endpoint — same code path Business runs (§2)
    "status_pages",     # the basic page; branding/custom-domain is Business
    "jackil_integration",
})

#: Business features a license can grant. Keep in sync with
#: ``sqsy_license.BUSINESS_FEATURES`` in the Mercantil repo.
# Civil SSO must never consult this flag — it ships free on both tiers.
BUSINESS_FEATURES = frozenset({
    "sites",
    "audit_log",
    "rbac_advanced",    # OPERATOR + custom roles; Free has ADMIN + VIEWER
    "branding",
    "status_branding",  # branded/public/custom-domain status pages
    "sso",              # external IdPs (SAML/OIDC) only — Civil SSO is free on both tiers
    "dashboard_sharing",  # read-only sharing of a dashboard; the flag lives in core
    "compliance_reports",  # per-site patch compliance, CSV export, branded report
    "jackil",             # bundled ticketing; the Jackil side verifies this same blob
})

#: Free-tier limits (soft — exceeded means a banner, never a block).
FREE_SEATS = 2   # 2 technicians (Admin/Operator); Viewers are free and unlimited
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
    lid: str = ""  # licence id, so a key posted publicly traces back to the buyer
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
            lid=str(d.get("lid") or ""),
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
# Refresh from Mercantil
#
# A Business license for a connected install runs about five weeks and is
# renewed here, so a copy of it stops working within a month. Only an install
# with a license ever calls out: free installs send nothing. Each request is
# signed with this install's key (apps.licensing.models.InstallKey), which
# Mercantil binds to the license on the first refresh and refuses for any
# other install. Every failure leaves the current license exactly as it was.

#: Renew once the license is this close to expiry.
REFRESH_WITHIN_DAYS = 21
REFRESH_TIMEOUT = 15

#: refresh_license results. "renewed"/"current" mean this install holds the
#: license; anything else is retried on the next run.
SETTLED = ("renewed", "current")


def refresh_message(instance: str, ts: int, version: str, seats: str) -> bytes:
    """What Mercantil verifies (its licenses.views.refresh_message)."""
    return f"vigil-license-refresh\n{instance}\n{ts}\n{version}\n{seats}".encode()


def _record(row, result: str, detail: str, lid: str) -> dict:
    from django.utils.timezone import now

    row.refreshed_at = now()
    row.refresh_result = result
    row.refresh_detail = detail[:300]
    row.refreshed_lid = lid
    row.save(update_fields=["refreshed_at", "refresh_result", "refresh_detail", "refreshed_lid"])
    return {"result": result, "detail": detail}


def refresh_enabled() -> bool:
    return bool(getattr(settings, "VIGIL_LICENSE_REFRESH", False))


def refresh_due(state: LicenseState, row, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    claims = state.claims
    if claims is None:
        return False
    return (row.refreshed_lid != claims.lid            # a newly applied license: claim it
            or row.refresh_result not in SETTLED        # not held yet, or it failed
            or claims.exp - now < REFRESH_WITHIN_DAYS * 86400)


def refresh_license(*, force: bool = False) -> dict:
    """Ask Mercantil for a newer license and apply it if it verifies.

    Returns {"result", "detail"}. Never raises and never weakens the license
    in place: a refusal, an outage or a bad answer is recorded and the
    current license stays until it expires as it always would have.
    """
    try:
        return _refresh_license(force=force)
    except Exception:  # noqa: BLE001 — licensing must never take anything down
        logger.exception("license refresh failed")
        return {"result": "error", "detail": "Refresh failed; see the logs."}


def _refresh_license(*, force: bool) -> dict:
    import requests

    from apps.licensing.models import InstallKey

    if not refresh_enabled():
        return {"result": "off", "detail": "Refresh is off (VIGIL_LICENSE_REFRESH)."}
    state = current_state()
    if state.claims is None or state.status in (Status.MISMATCH, Status.INVALID):
        return {"result": "no-license", "detail": "There is no license to refresh."}
    if state.source == "env":
        return {"result": "env", "detail": ("The license comes from VIGIL_LICENSE_KEY, which a "
                                            "refresh cannot replace. Paste it in Settings instead.")}
    row = InstallKey.get()
    lid = state.claims.lid
    if not force and not refresh_due(state, row):
        return {"result": row.refresh_result, "detail": row.refresh_detail}

    instance, ts = instance_id(), int(time.time())
    version, seats = str(getattr(settings, "VIGIL_VERSION", "")), str(seats_used())
    signer = row.signing_key()
    b64 = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    try:
        resp = requests.get(
            f"{settings.VIGIL_LICENSE_SERVER}/license",
            params={"instance": instance, "version": version, "seats": seats, "ts": ts,
                    "key": b64(bytes(signer.verify_key)),
                    "sig": b64(signer.sign(refresh_message(instance, ts, version, seats)).signature)},
            timeout=REFRESH_TIMEOUT,
        )
    except requests.RequestException as exc:
        return _record(row, "unreachable", f"Could not reach the license server: {exc.__class__.__name__}.", lid)

    if resp.status_code == 409:
        return _record(row, "refused", (
            "Another install holds this license, so it is not renewing here. If this is "
            "your install, move the license to it at susquehannasyntax.com/license."), lid)
    if resp.status_code != 200:
        return _record(row, "unreachable", f"The license server answered {resp.status_code}.", lid)
    try:
        blob = str(resp.json()["license"])
    except (ValueError, KeyError, TypeError):
        return _record(row, "unreachable", "The license server sent something unreadable.", lid)

    new = _classify(blob, "db")
    if new.status not in (Status.VALID, Status.GRACE) or new.claims is None:
        # A license that does not verify here, or is for another instance,
        # never replaces one that does.
        return _record(row, "rejected", f"The license server's answer did not verify: {new.detail}", lid)
    if new.claims.exp <= state.claims.exp:
        return _record(row, "current", "This install holds the license; nothing newer yet.", lid)
    set_license(blob)
    return _record(row, "renewed", "Renewed.", new.claims.lid)


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

        def __init__(self, detail=None, code=None):
            super().__init__(detail, code)
            # DRF runs the detail through ErrorDetail, which is a str subclass,
            # so `"licensed": False` rendered as the string "False" — truthy in
            # JavaScript, and therefore read by a client as *licensed*. The
            # endpoints that build the body themselves (statuspage, accounts)
            # answered with a real boolean, so the same field disagreed with
            # itself depending on which gate refused. DRF's handler passes a
            # plain dict through untouched, so the body is re-set after
            # super() rather than coerced.
            self.detail = upgrade_body(name)

    class _HasLicensedFeature(BasePermission):
        message = upgrade_body(name)

        def has_permission(self, request, view):
            if has_feature(name):
                return True
            raise _PaymentRequired()

    _HasLicensedFeature.__name__ = f"Requires_{name}"
    return _HasLicensedFeature


def licence_gate(request, name: str):
    """Return a 402 Response when *name* is not licensed, else None.

    The same six lines had been written out in sites, branding and dashboards,
    differing only in the feature name. A gate copied per app is a gate that
    drifts per app.
    """
    from rest_framework.exceptions import APIException
    from rest_framework.response import Response

    perm = require_feature(name)()
    try:
        perm.has_permission(request, None)
    except APIException as exc:
        return Response(exc.detail, status=402)
    return None


# --------------------------------------------------------------------------
# Seats (§6: whatever holds the users counts them; Vigil reads its own view)

def seats_used() -> int:
    """How many seats are in use. A seat is a technician — an Admin or
    Operator. Viewers are free and unlimited on both tiers."""
    try:
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        from apps.accounts.models import Role

        return (
            get_user_model().objects
            .filter(is_active=True)
            .filter(Q(is_superuser=True) | Q(is_staff=True)
                    | Q(profile__role__in=[Role.ADMIN, Role.OPERATOR]))
            .distinct()
            .count()
        )
    except Exception:  # noqa: BLE001
        logger.exception("seat count unavailable")
        return 0


def seats_allowed() -> int:
    state = current_state()
    if state.business_active and state.claims:
        return state.claims.seats
    return FREE_SEATS


def relays_allowed() -> int:
    """Community gets one connection point: the built-in relay or one remote relay.
    Business gets seats + 2. Enabled relays, the built-in one included, count."""
    state = current_state()
    if state.business_active and state.claims:
        return state.claims.seats + 2
    return 1


FREE_ADMINS = 2


def admin_cap() -> int | None:
    """How many Admin accounts this install may have, or None for unlimited.

    Free stops at two Admins — enforced when an account is created or promoted,
    never anywhere else. Business is unlimited and bills per technician seat.
    """
    return None if current_state().business_active else FREE_ADMINS


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
