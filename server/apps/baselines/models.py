"""Baselines — task definitions that auto-dispatch to newly approved hosts.

Free for everyone (folded from the never-shipped Pro tier, 2026.4.0). The
2FA that normally guards deployment happens at *baseline creation* instead of
dispatch time: an admin authorizing "every new host gets this" once is the
authorization for each future enrollment. High-risk definitions and
``update_agent`` steps are excluded — anything that replaces executables or
carries high risk keeps the human + 2FA in the loop, per the security model.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models


class Baseline(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(
        "tasks.TaskDefinition", on_delete=models.CASCADE, related_name="baselines",
    )
    # Optional tag filter: only hosts carrying at least one of these tags
    # receive the baseline (empty = every approved host).
    target_tags = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="baselines",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"baseline:{self.definition.name}"

    def matches(self, host) -> bool:
        if not self.enabled:
            return False
        if not self.target_tags:
            return True
        host_tags = {str(t).lower() for t in (host.tags or [])}
        return not host_tags.isdisjoint({str(t).lower() for t in self.target_tags})


def eligible(definition) -> tuple[bool, str]:
    """Whether *definition* may be a baseline. Mirrors the deploy path's
    packaging rules: no high risk, no update_agent (digest stamping and the
    2FA ceremony stay human-driven)."""
    if definition.risk_level == definition.RiskLevel.HIGH:
        return False, "high-risk definitions cannot be baselines"
    actions = (definition.parsed_spec or {}).get("actions") or []
    if any(a.get("type") == "update_agent" for a in actions):
        return False, "update_agent steps cannot be baselines"
    return True, ""


def dispatch_to_host(host, *, baselines=None) -> int:
    """Create pending tasks on *host* for every matching baseline.

    Called from the host_approved hook. Never raises — enrollment approval
    must succeed even if a baseline is broken; failures are logged.
    """
    import logging

    from apps.tasks.models import Task

    logger = logging.getLogger("vigil.baselines")
    created = 0
    rows = baselines if baselines is not None else (
        Baseline.objects.filter(enabled=True).select_related("definition"))
    for baseline in rows:
        try:
            if not baseline.matches(host):
                continue
            definition = baseline.definition
            ok, why = eligible(definition)
            if not ok:
                logger.warning("skipping baseline %s: %s", baseline.pk, why)
                continue
            spec = definition.parsed_spec or {}
            actions = spec.get("actions") or []
            if not actions:
                continue
            success_criteria = spec.get("success_criteria") or None
            steps = []
            for i, action in enumerate(actions):
                step = {
                    "id": action.get("id") or f"step{i + 1}",
                    "action": action["type"],
                    "params": action.get("params") or {},
                }
                if action.get("when"):
                    step["when"] = action["when"]
                if success_criteria:
                    step["success_criteria"] = success_criteria
                steps.append(step)
            Task.objects.create(
                host=host,
                requested_by=baseline.created_by,
                step_label=f"baseline: {definition.name}",
                action="_script",
                params={"steps": steps},
                risk_level=spec.get("risk", "standard"),
                state=Task.State.PENDING,
                nonce=secrets.token_hex(32),
            )
            created += 1
        except Exception:  # noqa: BLE001
            logger.exception("baseline %s failed for host %s", baseline.pk, host.pk)
    return created
