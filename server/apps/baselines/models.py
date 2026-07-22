"""Baselines — named sequences of task definitions that auto-dispatch to
newly approved hosts, and are callable from any task via ``type: baseline``.

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
    # The callable identity: `type: baseline, params: {name: ...}` resolves
    # case-insensitively against this.
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True, default="")
    # Optional tag filter: only hosts carrying at least one of these tags
    # receive the baseline at enrollment (empty = every approved host).
    target_tags = models.JSONField(default=list, blank=True)
    # enabled gates AUTO-ENROLL dispatch only; a disabled baseline is still
    # callable from tasks (a function you no longer auto-run is still a
    # function).
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="baselines",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"baseline:{self.name}"

    def matches(self, host) -> bool:
        if not self.enabled:
            return False
        if not self.target_tags:
            return True
        host_tags = {str(t).lower() for t in (host.tags or [])}
        return not host_tags.isdisjoint({str(t).lower() for t in self.target_tags})


class BaselineStep(models.Model):
    """One task definition in a baseline's sequence."""

    baseline = models.ForeignKey(Baseline, on_delete=models.CASCADE,
                                 related_name="steps")
    definition = models.ForeignKey("tasks.TaskDefinition",
                                   on_delete=models.CASCADE,
                                   related_name="baseline_steps")
    order = models.PositiveIntegerField(default=0)
    # Per-step input overrides: {"<action_index>": {"<param>": value}} merged
    # over the definition's action params at dispatch, so one shared task can
    # run with different inputs in different baselines.
    params_override = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("order",)
        constraints = [
            models.UniqueConstraint(fields=("baseline", "order"),
                                    name="uniq_baseline_step_order"),
        ]

    def __str__(self) -> str:
        return f"{self.baseline.name}[{self.order}] = {self.definition.name}"


def eligible(definition) -> tuple[bool, str]:
    """Whether *definition* may be part of a baseline. Mirrors the deploy
    path's packaging rules: no high risk, no update_agent (digest stamping
    and the 2FA ceremony stay human-driven)."""
    if definition.risk_level == definition.RiskLevel.HIGH:
        return False, "high-risk definitions cannot be baselines"
    actions = (definition.parsed_spec or {}).get("actions") or []
    if any(a.get("type") == "update_agent" for a in actions):
        return False, "update_agent steps cannot be baselines"
    return True, ""


def build_agent_steps(baseline: "Baseline") -> tuple[list[dict], str]:
    """The concrete agent steps for a baseline's whole sequence, with any
    nested ``type: baseline`` calls expanded. Returns ``(steps, max_risk)``."""
    from .expansion import expand_actions

    steps: list[dict] = []
    max_risk = "low"
    i = 0
    for step in baseline.steps.select_related("definition").order_by("order"):
        spec = step.definition.parsed_spec or {}
        actions_src = spec.get("actions") or []
        override = step.params_override or {}
        if override:
            actions_src = [
                {**a, "params": {**(a.get("params") or {}),
                                 **override.get(str(idx), {})}}
                for idx, a in enumerate(actions_src)
            ]
        actions, risk = expand_actions(actions_src)
        success_criteria = spec.get("success_criteria") or None
        for action in actions:
            i += 1
            agent_step = {
                "id": f"step{i}",
                "action": action["type"],
                "params": action.get("params") or {},
            }
            if action.get("when"):
                agent_step["when"] = action["when"]
            if success_criteria:
                agent_step["success_criteria"] = success_criteria
            steps.append(agent_step)
        from .expansion import _max_risk
        max_risk = _max_risk(max_risk, spec.get("risk", "standard"))
        max_risk = _max_risk(max_risk, risk)
    return steps, max_risk


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
        Baseline.objects.filter(enabled=True).prefetch_related("steps__definition"))
    for baseline in rows:
        try:
            if not baseline.matches(host):
                continue
            bad = [s.definition.name for s in baseline.steps.all()
                   if not eligible(s.definition)[0]]
            if bad:
                logger.warning("skipping baseline %s: ineligible definitions %s",
                               baseline.name, bad)
                continue
            steps, risk = build_agent_steps(baseline)
            if not steps:
                continue
            Task.objects.create(
                host=host,
                requested_by=baseline.created_by,
                step_label=f"baseline: {baseline.name}",
                action="_script",
                params={"steps": steps},
                risk_level=risk,
                state=Task.State.PENDING,
                nonce=secrets.token_hex(32),
            )
            created += 1
        except Exception:  # noqa: BLE001
            logger.exception("baseline %s failed for host %s", baseline.pk, host.pk)
    return created
