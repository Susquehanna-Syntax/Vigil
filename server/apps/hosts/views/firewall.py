"""apps.hosts.views: firewall views (split from the old single module, unchanged)."""
from drf_spectacular.utils import extend_schema

from ..api_schema import (
    HostsErrorSerializer,
    HostsFirewallApplySerializer,
    HostsFirewallSerializer,
    HostsFirewallTaskSerializer,
)
from ._common import *  # noqa: F401,F403


@extend_schema(responses={200: HostsFirewallSerializer, 404: HostsErrorSerializer})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def host_firewall(request, host_id):
    """Return the last firewall snapshot read from this host.

    Not gated — it changes nothing on the host, so the Firewall tab can
    render before anyone has typed a 2FA code.
    """
    from ..models import HostFirewall

    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied

    fw = HostFirewall.objects.filter(host=host).first()
    if fw is None:
        # No read has ever landed. ``enabled`` stays None (unknown), not
        # False — collapsing "never read" to "disabled" is the exact
        # confident-wrong-answer failure the tri-state exists to prevent.
        return Response({
            "tool": "", "supported": True, "enabled": None,
            "defaults": {}, "profiles": [], "rules": [], "unparsed": [],
            "fetched_at": None,
        })
    return Response({
        "tool": fw.tool, "supported": fw.supported, "enabled": fw.enabled,
        "defaults": fw.defaults, "profiles": fw.profiles, "rules": fw.rules,
        "unparsed": fw.unparsed, "fetched_at": fw.fetched_at,
    })


@extend_schema(request=None, responses={202: HostsFirewallTaskSerializer,
                                        400: HostsErrorSerializer, 403: HostsErrorSerializer,
                                        404: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def host_firewall_refresh(request, host_id):
    """Queue a read of this host's firewall. Not 2FA-gated: it changes nothing."""
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied

    if host.mode == Host.Mode.MONITOR:
        return Response(
            {"error": "Host is in monitor mode — it does not execute tasks"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Don't stack duplicate reads — if one is already queued or in flight,
    # point the caller at it instead of firing off another.
    existing = Task.objects.filter(
        host=host, action="list_firewall_rules",
        state__in=[Task.State.PENDING, Task.State.DISPATCHED, Task.State.EXECUTING],
    ).first()
    if existing:
        return Response({"task_id": str(existing.id), "queued": False},
                        status=status.HTTP_202_ACCEPTED)

    task = _queue_firewall_task(host, request.user, "list_firewall_rules",
                                {}, Task.RiskLevel.LOW)
    return Response({"task_id": str(task.id), "queued": True},
                    status=status.HTTP_202_ACCEPTED)


@extend_schema(request=HostsFirewallApplySerializer,
               responses={202: HostsFirewallTaskSerializer, 400: HostsErrorSerializer,
                          401: HostsErrorSerializer, 403: HostsErrorSerializer,
                          404: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def host_firewall_apply(request, host_id):
    """Apply one firewall change. Allowlist- and lockout-guarded, then 2FA-gated.

    Order matters and is deliberate: the allowlist check and the lockout
    guard both run before TOTP is checked. The refusal is about the change,
    not the credential — making someone fetch a code and only then telling
    them no is rude and teaches people to ignore the dialog.
    """
    from apps.accounts.totp import require_totp_confirmation

    from ..firewall_guard import check_change
    from ..models import HostFirewall

    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    # Before the allowlist and lockout guards, because those are about the
    # change and this is about the caller: someone who may not touch the
    # firewall should not learn which changes would have been refused.
    if not can(request.user, scoping.scope_of(host), "hosts", "firewall"):
        return Response({"error": "Not permitted"}, status=status.HTTP_403_FORBIDDEN)

    if host.mode == Host.Mode.MONITOR:
        return Response(
            {"error": "Host is in monitor mode — it does not execute tasks"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    action = (request.data.get("action") or "").strip()
    if action not in _FIREWALL_ACTIONS:
        return Response(
            {"error": f"action must be one of: {', '.join(sorted(_FIREWALL_ACTIONS))}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    params = request.data.get("params") or {}

    # Guard before TOTP — see docstring.
    fw = HostFirewall.objects.filter(host=host).first()
    snapshot = _firewall_snapshot_dict(fw) if fw is not None else None
    refusal = check_change(host, snapshot, {"action": action, "params": params})
    if refusal:
        return Response({"error": refusal}, status=status.HTTP_400_BAD_REQUEST)

    error = require_totp_confirmation(request.user, request.data)
    if error:
        return Response({"error": error}, status=status.HTTP_401_UNAUTHORIZED)

    task = _queue_firewall_task(host, request.user, action, params, Task.RiskLevel.HIGH)
    return Response({"task_id": str(task.id)}, status=status.HTTP_201_CREATED)
