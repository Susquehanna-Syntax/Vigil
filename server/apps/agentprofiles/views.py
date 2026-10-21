"""The admin API: profile CRUD, the action picker, and minting config tokens."""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.tasks.refusals import AGENT_ACTIONS
from apps.tasks.registry import ACTION_REGISTRY

from . import render
from .models import AgentProfile, EnrollConfig
from .render import NEVER_ALLOWLISTABLE

CONFIG_TOKEN_TTL = timedelta(hours=24)


def _audit(request, action: str, target: str) -> None:
    """Audit a mutation. Core must not hard-depend on apps_business."""
    try:
        from apps_business.audits.models import record
    except ImportError:
        return
    record(action, user=request.user, target=target)


def _fields(profile: AgentProfile) -> dict:
    return {
        "mode": profile.mode,
        "allowlist": list(profile.allowlist or []),
        "primary_relay": str(profile.primary_relay_id) if profile.primary_relay_id else None,
        "fallback_relays": list(profile.fallback_relays or []),
        "include_dashboard_fallback": profile.include_dashboard_fallback,
        "allow_remote_shell": profile.allow_remote_shell,
        "allow_remote_desktop": profile.allow_remote_desktop,
        "remote_shell_user": profile.remote_shell_user,
        "remote_desktop_port": profile.remote_desktop_port,
        "allow_reprovision": profile.allow_reprovision,
        "tags": list(profile.tags or []),
        "process_watch": list(profile.process_watch or []),
        "checkin_interval": profile.checkin_interval,
        "docker_check_interval": profile.docker_check_interval,
        "software_interval": profile.software_interval,
    }


def _row(profile: AgentProfile) -> dict:
    return {
        "id": str(profile.id),
        "name": profile.name,
        "description": profile.description,
        "builtin": profile.builtin,
        **_fields(profile),
        "install_profiles": [p.name for p in profile.install_profiles.all()],
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def dashboard_url(request) -> str:
    """The address agents should dial to reach this dashboard."""
    public = (getattr(settings, "VIGIL_PUBLIC_URL", "") or "").strip().rstrip("/")
    return public or f"{request.scheme}://{request.get_host()}"


def _apply(profile: AgentProfile | None, data: dict) -> tuple[dict, dict]:
    """Profile fields overlaid with the request's, then validated."""
    base = _fields(profile) if profile else {}
    if profile:
        base["name"] = profile.name
        base["description"] = profile.description
    for key in list(data or {}):
        if key in render.FIELDS:
            base[key] = data[key]
    return render.validate(base)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def profile_index(request):
    if request.method == "GET":
        qs = AgentProfile.objects.prefetch_related("install_profiles")
        return Response([_row(p) for p in qs])

    clean, errors = _apply(None, request.data)
    if errors:
        return Response({"errors": errors}, status=400)
    profile = AgentProfile(
        name=clean["name"], description=clean["description"],
        mode=clean["mode"], builtin=False,
        primary_relay_id=clean["primary_relay"],
        allowlist=clean["allowlist"],
        fallback_relays=clean["fallback_relays"],
        include_dashboard_fallback=clean["include_dashboard_fallback"],
        allow_remote_shell=clean["allow_remote_shell"],
        allow_remote_desktop=clean["allow_remote_desktop"],
        remote_shell_user=clean["remote_shell_user"],
        remote_desktop_port=clean["remote_desktop_port"],
        allow_reprovision=clean["allow_reprovision"],
        tags=clean["tags"], process_watch=clean["process_watch"],
        checkin_interval=clean["checkin_interval"],
        docker_check_interval=clean["docker_check_interval"],
        software_interval=clean["software_interval"])
    profile.save()
    _audit(request, "agentprofile.created", profile.name)
    return Response(_row(profile), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def profile_detail(request, profile_id):
    profile = get_object_or_404(AgentProfile, pk=profile_id)

    if request.method == "GET":
        return Response(_row(profile))

    if request.method == "DELETE":
        # Builtin profiles are the wizard's starting points. Edit them freely;
        # deleting one leaves a wizard with nothing to copy from.
        if profile.builtin:
            return Response({"error": "Built-in profiles cannot be deleted"},
                            status=400)
        name = profile.name
        profile.delete()
        _audit(request, "agentprofile.deleted", name)
        return Response(status=204)

    clean, errors = _apply(profile, request.data)
    if errors:
        return Response({"errors": errors}, status=400)
    profile.name = clean["name"]
    profile.description = clean["description"]
    profile.mode = clean["mode"]
    profile.allowlist = clean["allowlist"]
    profile.primary_relay_id = clean["primary_relay"]
    profile.fallback_relays = clean["fallback_relays"]
    profile.include_dashboard_fallback = clean["include_dashboard_fallback"]
    profile.allow_remote_shell = clean["allow_remote_shell"]
    profile.allow_remote_desktop = clean["allow_remote_desktop"]
    profile.remote_shell_user = clean["remote_shell_user"]
    profile.remote_desktop_port = clean["remote_desktop_port"]
    profile.allow_reprovision = clean["allow_reprovision"]
    profile.tags = clean["tags"]
    profile.process_watch = clean["process_watch"]
    profile.checkin_interval = clean["checkin_interval"]
    profile.docker_check_interval = clean["docker_check_interval"]
    profile.software_interval = clean["software_interval"]
    profile.save()
    _audit(request, "agentprofile.updated", profile.name)
    return Response(_row(profile))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsAdmin])
def actions_list(request):
    """Every action the agent knows, grouped like the wiki.

    The wizard's allowlist picker reads this rather than hardcoding a list that
    drifts from the registry the moment an action is added.
    """
    from apps.tasks.management.commands.render_wiki_actions import GROUPS

    group_of: dict[str, str] = {}
    for key, _title, names in GROUPS:
        for name in names:
            group_of[name] = key

    out = []
    for name in sorted(AGENT_ACTIONS):
        entry = ACTION_REGISTRY.get(name, {})
        out.append({
            "name": name,
            "label": entry.get("label", name),
            "group": group_of.get(name, "Other"),
            "risk": entry.get("risk", "standard"),
            "allowlistable": name not in NEVER_ALLOWLISTABLE,
        })
    return Response(out)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def enroll_config(request):
    """Mint a one-time config token for a profile plus one-off overrides."""
    profile = None
    if request.data.get("profile"):
        if render._as_uuid(request.data["profile"]) is None:
            return Response({"errors": {"profile": "must be a profile id"}},
                            status=400)
        profile = get_object_or_404(AgentProfile, pk=request.data["profile"])

    overrides = request.data.get("overrides") or {}
    if not isinstance(overrides, dict):
        return Response({"errors": {"overrides": "must be an object"}},
                        status=400)

    # A one-off config has no profile to borrow a name from, and the name is
    # never written to agent.yml: give validate() one so it can't fail on it.
    if profile is None and not overrides.get("name"):
        overrides = {**overrides, "name": "custom"}
    clean, errors = _apply(profile, overrides)
    if errors:
        return Response({"errors": errors}, status=400)

    settings_out = render.agent_settings(clean, dashboard_url(request))
    token = secrets.token_urlsafe(32)
    config = EnrollConfig.objects.create(
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        profile=profile,
        settings=settings_out,
        created_by=request.user if request.user.is_authenticated else None,
        expires_at=now() + CONFIG_TOKEN_TTL)

    _audit(request, "agentprofile.enroll_config_created",
           profile.name if profile else clean["name"] or "defaults")
    # The raw token exists in this response and nowhere else.
    return Response({
        "id": str(config.id),
        "token": token,
        "expires_at": config.expires_at.isoformat(),
        "server_url": settings_out["server_url"],
        "settings": settings_out,
        "notes": clean.get("_notes", []),
    }, status=201)
