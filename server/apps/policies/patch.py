"""The Patching tab: which updates a policy installs, worked out at run time.

The KB lists are compiled when the policy runs, not when it is saved, so a
KB that came out of deferral overnight — or was declined this morning — is
in or out of tonight's window as it should be.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Min
from django.utils.timezone import now

from apps.software.models import PendingUpdate, UpdateDecision

WINDOWS = PendingUpdate.Kind.WINDOWS
LINUX = PendingUpdate.Kind.LINUX


def declined(kind=WINDOWS) -> set[str]:
    return set(UpdateDecision.objects.filter(
        kind=kind, decision=UpdateDecision.Decision.DECLINED).values_list("key", flat=True))


def _approved(kind=WINDOWS) -> set[str]:
    return set(UpdateDecision.objects.filter(
        kind=kind, decision=UpdateDecision.Decision.APPROVED).values_list("key", flat=True))


def eligible_kbs(policy, at=None) -> list[str] | None:
    """KBs past the policy's deferral (first seen anywhere in the fleet at
    least ``deferral_days`` ago) or approved, minus the declined ones.

    None when there is no deferral — then every KB the classifications let
    through is eligible, and no include list is needed.
    """
    if not policy.deferral_days:
        return None
    cutoff = (at or now()) - timedelta(days=policy.deferral_days)
    aged = {key for key, first in PendingUpdate.objects.filter(kind=WINDOWS)
            .values("key").annotate(first=Min("first_seen")).values_list("key", "first")
            if first <= cutoff and key.upper().startswith("KB")}
    approved = {k for k in _approved() if k.upper().startswith("KB")}
    return sorted((aged | approved) - declined())


def _reboot_params(policy) -> dict | None:
    R = policy.Reboot
    if policy.reboot == R.IN_WINDOW:
        return {"delay_seconds": 300, "notify": True,
                "notify_message": "Updates were installed. This computer restarts in 5 minutes."}
    if policy.reboot == R.ASK:
        return {"delay_seconds": 300, "notify": True,
                "notify_message": "Updates need a restart. You can postpone it a few times.",
                "defer_limit": 3, "defer_minutes": 60}
    return None


def patch_steps(policy, at=None) -> list[dict]:
    if not policy.patch_enabled:
        return []
    steps = []
    include = eligible_kbs(policy, at)
    # A deferral with nothing yet eligible installs nothing on Windows — an
    # empty include list would mean "everything" to the agent.
    if include is None or include:
        params: dict = {}
        if policy.windows_classifications:
            # Comma-separated: signed task params are primitives only.
            params["classifications"] = ",".join(policy.windows_classifications)
        if include:
            params["include_kb"] = ",".join(include)
        exclude = sorted(declined())
        if exclude:
            params["exclude_kb"] = ",".join(exclude)
        steps.append({"id": "windows_updates", "type": "windows_update_install",
                      "when": 'agent.os == "windows"', "params": params})
        reboot = _reboot_params(policy)
        if reboot:
            steps.append({
                "id": "reboot", "type": "reboot",
                "when": ('agent.os == "windows" and '
                         "steps.windows_updates.result.reboot_required == True"),
                "params": reboot})
    steps.append({"id": "linux_updates", "type": "run_package_updates",
                  "when": 'agent.os == "linux"',
                  "params": {"security_only": policy.linux_updates == policy.LinuxUpdates.SECURITY}})
    return steps


def patch_changes(policy, host_ids, at=None) -> dict:
    """``{host_id: [change, …]}`` for pending updates this policy would install."""
    if not policy.patch_enabled or not host_ids:
        return {}
    include = eligible_kbs(policy, at)
    include = set(include) if include is not None else None
    skip = declined()
    wanted = {c.lower() for c in policy.windows_classifications or []}
    out: dict = {}
    for row in PendingUpdate.objects.filter(host_id__in=host_ids).order_by("key"):
        if row.kind == WINDOWS:
            if row.key in skip:
                continue
            if include is not None and row.key not in include:
                continue
            if wanted and row.classification.lower() not in wanted:
                continue
            change = {"update": row.key, "kind": "windows", "title": row.title,
                      "action": "install_update", "severity": row.severity}
        else:
            change = {"update": row.key, "kind": "linux", "title": row.title,
                      "action": "install_update", "want": row.version}
        out.setdefault(str(row.host_id), []).append(change)
    return out
