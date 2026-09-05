"""Playbooks — named sequences of task definitions that auto-dispatch to
newly approved hosts, and are callable from any task via ``type: playbook``.

Free for everyone (folded from the never-shipped Pro tier, 2026.4.0). The
2FA that normally guards deployment happens at *playbook creation* instead of
dispatch time: an admin authorizing "every new host gets this" once is the
authorization for each future enrollment. High-risk definitions and
``update_agent`` steps are excluded — anything that replaces executables or
carries high risk keeps the human + 2FA in the loop, per the security model.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models

from apps.hosts.models import TagRowSyncMixin


class Playbook(TagRowSyncMixin, models.Model):

    tag_sync_fields = [("target_tags", "target_tag_rows")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The callable identity: `type: playbook, params: {name: ...}` resolves
    # case-insensitively against this.
    #
    # Not unique at the DB level: once playbooks are site-scoped, two sites may
    # each own a "Nightly patch scan". Uniqueness is enforced within a scope by
    # the view layer (see vigil/scoping.py).
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    # Optional tag filter: only hosts carrying at least one of these tags
    # receive the playbook at enrollment (empty = every approved host).
    target_tags = models.JSONField(default=list, blank=True)
    #: Row-backed mirror of ``target_tags`` — see Host.tag_rows.
    target_tag_rows = models.ManyToManyField("hosts.Tag", blank=True,
                                             related_name="playbooks")
    # enabled gates AUTO-ENROLL dispatch only; a disabled playbook is still
    # callable from tasks (a function you no longer auto-run is still a
    # function).
    enabled = models.BooleanField(default=True)
    # Opt-in to high-risk steps. Off by default, and turning it ON requires a
    # fresh TOTP code — that confirmation IS the 2FA for every future
    # unattended dispatch, exactly as playbook creation is for standard-risk
    # steps. Interactively a high-risk task costs 2FA plus a 60-second delay;
    # here nobody is watching, so the authorization has to happen once, in
    # advance, and deliberately. update_agent stays excluded regardless: it
    # replaces the executable that enforces the agent's own allowlist.
    allow_high_risk = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="playbooks",
    )
    #: Stable identity for this item in the community catalog, independent of
    #: its name. Set when the item is forked from the catalog (copied out of
    #: the file's ``uid``), or minted the first time it is exported.
    #:
    #: Community files used to reference each other by slug — the filename,
    #: which the catalog does not force to match the slugified ``name``. That
    #: made references break on a rename and collide between two items that
    #: happened to be called the same thing. A uid is neither: it survives a
    #: rename and it does not require names to be unique.
    #:
    #: Nullable because everything that predates it, and everything never
    #: shared, legitimately has none.
    community_uid = models.UUIDField(null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"playbook:{self.name}"

    def matches(self, host) -> bool:
        if not self.enabled:
            return False
        # No target tags means every host, which is the opposite of a wave
        # with no tags. Long-standing asymmetry, pinned by test_tag_semantics.
        if not self.target_tags:
            return True
        # An unsaved instance has no rows, and its strings are the only truth
        # it has. Falling back keeps an in-memory Playbook comparing correctly
        # instead of silently matching nothing — which is what a row-only
        # implementation would do, and is a nasty thing to debug.
        # NOT `self.pk is None`: the primary key is a UUIDField with a
        # default, so an unsaved instance already has one. `_state.adding` is
        # the only reliable "has this been written yet" check here.
        if self._state.adding or host._state.adding:
            host_tags = {str(t).lower() for t in (host.tags or [])}
            return not host_tags.isdisjoint(
                {str(t).lower() for t in self.target_tags})
        # Compare row ids rather than strings. The row key already encodes the
        # comparison — lowercase, whitespace preserved — so this matches
        # exactly what the string version matched.
        wanted = set(self.target_tag_rows.values_list("id", flat=True))
        if not wanted:
            return False
        return bool(wanted & set(host.tag_rows.values_list("id", flat=True)))


class PlaybookStep(models.Model):
    """One task definition in a playbook's sequence."""

    playbook = models.ForeignKey(Playbook, on_delete=models.CASCADE,
                                 related_name="steps")
    definition = models.ForeignKey("tasks.TaskDefinition",
                                   on_delete=models.CASCADE,
                                   related_name="playbook_steps")
    order = models.PositiveIntegerField(default=0)
    # Per-step input overrides: {"<action_index>": {"<param>": value}} merged
    # over the definition's action params at dispatch, so one shared task can
    # run with different inputs in different playbooks.
    params_override = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("order",)
        constraints = [
            models.UniqueConstraint(fields=("playbook", "order"),
                                    name="uniq_playbook_step_order"),
        ]

    def __str__(self) -> str:
        return f"{self.playbook.name}[{self.order}] = {self.definition.name}"


def eligible(definition, *, allow_high_risk: bool = False) -> tuple[bool, str]:
    """Whether *definition* may be part of a playbook. Mirrors the deploy
    path's packaging rules: no high risk unless the playbook opted in, and
    never update_agent (digest stamping and the 2FA ceremony stay
    human-driven).

    *allow_high_risk* is the playbook's own flag, which an admin can only set
    by passing a TOTP challenge — see Playbook.allow_high_risk. Callers must
    pass it from the playbook being validated, never hardcode True: the
    default is what keeps an un-opted-in playbook safe.
    """
    if definition.risk_level == definition.RiskLevel.HIGH and not allow_high_risk:
        return False, "high-risk definitions cannot be playbooks"
    actions = (definition.parsed_spec or {}).get("actions") or []
    if any(a.get("type") == "update_agent" for a in actions):
        return False, "update_agent steps cannot be playbooks"
    return True, ""


def build_agent_steps(playbook: "Playbook") -> tuple[list[dict], str]:
    """The concrete agent steps for a playbook's whole sequence, with any
    nested ``type: playbook`` calls expanded. Returns ``(steps, max_risk)``."""
    from .expansion import expand_actions

    steps: list[dict] = []
    max_risk = "low"
    i = 0
    for step in playbook.steps.select_related("definition").order_by("order"):
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


def dispatch_to_host(host, *, playbooks=None) -> int:
    """Create pending tasks on *host* for every matching playbook.

    Called from the host_approved hook. Never raises — enrollment approval
    must succeed even if a playbook is broken; failures are logged.
    """
    import logging

    from apps.tasks.models import Task, TaskRun

    logger = logging.getLogger("vigil.playbooks")
    created = 0
    rows = playbooks if playbooks is not None else (
        Playbook.objects.filter(enabled=True).prefetch_related("steps__definition"))
    for playbook in rows:
        try:
            if not playbook.matches(host):
                continue
            bad = [s.definition.name for s in playbook.steps.all()
                   if not eligible(s.definition,
                                   allow_high_risk=playbook.allow_high_risk)[0]]
            if bad:
                logger.warning("skipping playbook %s: ineligible definitions %s",
                               playbook.name, bad)
                continue
            steps, risk = build_agent_steps(playbook)
            if not steps:
                continue
            # One run per playbook per host: enrollment dispatch is per-host by
            # nature, and a run keeps the result visible in history.
            run = TaskRun.objects.create(
                source=TaskRun.Source.PLAYBOOK,
                playbook=playbook,
                name_snapshot=playbook.name[:120],
                requested_by=playbook.created_by,
                host_count=1,
                step_count=len(steps),
            )
            Task.objects.create(
                host=host,
                run=run,
                requested_by=playbook.created_by,
                step_label=f"playbook: {playbook.name}",
                action="_script",
                params={"steps": steps},
                risk_level=risk,
                state=Task.State.PENDING,
                nonce=secrets.token_hex(32),
            )
            created += 1
        except Exception:  # noqa: BLE001
            logger.exception("playbook %s failed for host %s", playbook.pk, host.pk)
    return created
