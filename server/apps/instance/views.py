"""Read and write the settings an operator may change without a restart.

Admin-only, both ways. These fields hold scanner credentials and an SMTP
password; being able to read back which of them are configured is already more
than a viewer should have, and being able to change where alert mail goes is a
way to redirect a security signal.
"""

from __future__ import annotations

from django.conf import settings as django_settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from vigil import hooks

from . import config, registry


def _field(key: registry.Key) -> dict:
    locked = config.env_locked(key.name)
    out = {
        "name": key.name,
        "kind": key.kind,
        "label": key.label,
        "help": key.help,
        "placeholder": key.placeholder,
        "choices": list(key.choices),
        "env_locked": locked,
        "source": config.source(key.name),
        "is_set": config.is_set(key.name),
    }
    if key.kind == registry.SECRET:
        # Never leaves the server, not even to an admin. "Is one stored" is
        # everything the form needs to render honestly.
        out["value"] = ""
    else:
        value = config.setting(key.name)
        out["value"] = "" if value is None else value
        out["default"] = getattr(django_settings, key.name, "")
    return out


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def instance_settings(request):
    """GET the settable surface; POST a partial dict of changes.

    POST body is ``{"values": {NAME: value, ...}, "clear": [NAME, ...]}``.
    A secret sent as an empty string is left untouched — that is what the form
    sends back when the admin did not retype it — so clearing one is an
    explicit entry in ``clear``.
    """
    if request.method == "GET":
        groups = []
        for gid, label, blurb in registry.GROUPS:
            groups.append({
                "id": gid,
                "label": label,
                "help": blurb,
                "fields": [_field(k) for k in registry.KEYS if k.group == gid],
            })
        return Response({"groups": groups})

    values = request.data.get("values") or {}
    to_clear = request.data.get("clear") or []
    if not isinstance(values, dict) or not isinstance(to_clear, list):
        return Response({"detail": "values must be an object and clear a list."},
                        status=400)

    unknown = [n for n in list(values) + list(to_clear) if n not in registry.BY_NAME]
    if unknown:
        return Response({"detail": f"unknown settings: {', '.join(sorted(unknown))}"},
                        status=400)

    errors = {}
    for name, raw in values.items():
        if config.env_locked(name):
            continue
        ok, message = config.validate(name, raw)
        if not ok:
            errors[name] = message
    if errors:
        return Response({"detail": "Some values were rejected.", "errors": errors},
                        status=400)

    changed = []
    for name, raw in values.items():
        if config.env_locked(name):
            continue
        before = config.setting(name)
        config.write(name, raw, user=request.user)
        if config.setting(name) != before:
            changed.append(name)
    for name in to_clear:
        if config.env_locked(name):
            continue
        config.clear(name)
        changed.append(name)

    if changed:
        # Names only. The audit trail records that the SMTP password changed,
        # never what it changed to.
        hooks.emit("instance_settings_changed", names=sorted(set(changed)),
                   changed_by=request.user)

    ignored = sorted(n for n in list(values) + list(to_clear)
                     if config.env_locked(n))
    return Response({"changed": sorted(set(changed)), "env_locked": ignored})


@api_view(["POST"])
@permission_classes([IsAdmin])
def test_integration(request):
    """Prove the saved credentials actually work, before a scan needs them.

    Each probe is the cheapest call that exercises authentication: a GMP
    ``<authenticate>``, a Nessus API-key request, an SMTP ``open()``. None of
    them create scans or send alert mail.
    """
    target = (request.data.get("target") or "").strip()
    if target == "greenbone":
        return Response(_test_greenbone())
    if target == "nessus":
        return Response(_test_nessus())
    if target == "email":
        return Response(_test_email(request))
    if target == "jackil":
        return Response(_test_jackil())
    return Response({"detail": "target must be greenbone, nessus, jackil or email."},
                    status=400)


def _test_greenbone() -> dict:
    from apps.vulns.scanners.greenbone import _GmpClient, _parse_gmp_url

    url = config.setting("GREENBONE_URL")
    username = config.setting("GREENBONE_USERNAME")
    password = config.setting("GREENBONE_PASSWORD")
    if not all([url, username, password]):
        return {"ok": False, "detail": "URL, username and password are all required."}
    try:
        host, port = _parse_gmp_url(url)
    except ValueError as exc:
        return {"ok": False, "detail": f"Bad URL: {exc}"}
    try:
        client = _GmpClient(host, port, config.setting("GREENBONE_VERIFY_SSL"))
    except Exception as exc:
        return {"ok": False, "detail": f"Could not connect to {host}:{port} — {exc}"}
    try:
        client.authenticate(username, password)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    finally:
        client.close()
    return {"ok": True, "detail": f"Authenticated to GMP at {host}:{port}."}


def _test_nessus() -> dict:
    import requests

    url = (config.setting("NESSUS_URL") or "").rstrip("/")
    access = config.setting("NESSUS_ACCESS_KEY")
    secret = config.setting("NESSUS_SECRET_KEY")
    if not all([url, access, secret]):
        return {"ok": False, "detail": "URL and both API keys are required."}
    try:
        resp = requests.get(
            f"{url}/scans",
            headers={"X-ApiKeys": f"accessKey={access};secretKey={secret}"},
            verify=config.setting("NESSUS_VERIFY_SSL"), timeout=20,
        )
    except Exception as exc:
        return {"ok": False, "detail": f"Could not reach {url} — {exc}"}
    if resp.status_code == 401:
        return {"ok": False, "detail": "Nessus rejected the API keys."}
    if resp.status_code >= 400:
        return {"ok": False, "detail": f"Nessus answered {resp.status_code}."}
    return {"ok": True, "detail": "API keys accepted."}


def _test_jackil() -> dict:
    """A one-row ticket listing. Deliberately not a create — a connection test
    must not leave a ticket behind in someone's queue."""
    from apps.jackil import client

    try:
        client.ping()
    except client.JackilError as exc:
        return {"ok": False, "detail": str(exc)}
    detail = f"Connected to {config.setting('JACKIL_URL')}."
    if not config.setting("JACKIL_ENABLED"):
        detail += " Tickets are still off — tick “Open tickets for alerts”."
    return {"ok": True, "detail": detail}


def _test_email(request) -> dict:
    connection = config.mail_connection()
    if "smtp" not in type(connection).__module__:
        # Opening the console backend always "succeeds" and proves nothing.
        # Saying so beats a green tick that means mail is still being printed
        # to the log rather than sent.
        return {"ok": False, "detail": (
            "Mail is not going over SMTP. Set an SMTP host here, or clear "
            "EMAIL_BACKEND from the environment if it is pinned there."
        )}
    try:
        connection.open()
        connection.close()
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": f"Connected to {config.setting('EMAIL_HOST')}."}
