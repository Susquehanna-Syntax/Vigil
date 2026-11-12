import json
import math
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from django.db import IntegrityError, transaction
from django.db.models import Count, F
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

#: How long a high-risk task is held before an agent may collect it. The tier
#: rule in CLAUDE.md is "high-risk (2FA + 60s delay)": the 2FA is the
#: confirmation at dispatch, and this is the delay — the window in which an
#: operator who has just realised they picked the wrong host, or the wrong tag,
#: can still stop it. Enforced by the check-in view, which withholds any task
#: whose not_before is in the future.
HIGH_RISK_HOLD_SECONDS = 60


from apps.accounts.permissions import IsAdmin
from apps.hosts.authentication import authenticate_agent
from apps.hosts.models import Host
from vigil import scoping

from ..models import HuntMatch, PatchRollout, Task, TaskDefinition, TaskRun
from ..rollout import (
    FAILURE_STATES,
    _emit_hunt_text_requested,
    _hunt_text_step_ids,
    halt_batch,
    halt_rollout,
    resume_rollout,
    start_rollout,
    start_rollout_batch,
)
from ..rollout_serializers import PatchRolloutSerializer
from ..serializers import (
    TaskDefinitionSerializer,
    TaskRunSerializer,
    TaskRunSummarySerializer,
    TaskSerializer,
)
from ..spec import (
    ACTION_REGISTRY,
    SpecError,
    _deploy_params,
    _validate_on_failure,
    _validate_schedule,
    _validate_success_criteria,
    parse_and_validate,
    resolve_inputs,
)

_TERMINAL_STATES = {
    Task.State.COMPLETED, Task.State.FAILED,
    Task.State.REJECTED, Task.State.SKIPPED,
    Task.State.NOT_APPLICABLE,
}
_UPDATABLE_STATES = {Task.State.DISPATCHED, Task.State.EXECUTING}

#: Characters kept at each end of a Trivy scan output once it has been
#: successfully ingested. A full report runs to megabytes and buries every
#: step that ran after it in the run-detail modal; the findings themselves
#: now live in VulnFinding rows, so the blob is redundant. Head and tail are
#: preserved because in a multi-step run they are other steps' output.
_TRIVY_OUTPUT_KEEP = 2000

#: Hard cap on the serialised step results stored on a Task. A runaway
#: script can otherwise grow result_data past what the run detail should
#: carry; over the cap the whole block is dropped with a note.
_MAX_RESULT_BYTES = 64 * 1024
_MAX_STEPS = 200
_MAX_RESULT_STR = 4096
_STEP_STATUSES = {"ok", "error", "skipped"}


#: Hard caps on stored hunt matches. A runaway hunt would otherwise fill the
#: database; per-step first, then the whole task.
_HUNT_MATCHES_PER_STEP = 5000
_HUNT_MATCHES_PER_TASK = 20000
_HUNT_FIELD_MAX = 1000
_HUNT_KEY_MAX = 60


# ── Agent-facing: task result ────────────────────────────────────────────────


#: How a finished task's state reads to a playbook branch condition.
_BRANCH_STATUS = {
    Task.State.COMPLETED: "ok",
    Task.State.SKIPPED: "skipped",
    Task.State.NOT_APPLICABLE: "not_applicable",
    Task.State.FAILED: "failed",
    Task.State.REJECTED: "failed",
    Task.State.EXPIRED: "failed",
}


_FAILURE_LIKE = {Task.State.FAILED, Task.State.REJECTED, Task.State.EXPIRED}


# ── TaskDefinition CRUD ──────────────────────────────────────────────────────


def _user_can_see(definition: TaskDefinition, user) -> bool:
    return (
        definition.visibility == TaskDefinition.Visibility.COMMUNITY
        or definition.owner_id == user.id
    )


def _save_definition_from_yaml(definition: TaskDefinition, yaml_source: str) -> None:
    spec = parse_and_validate(yaml_source)
    if spec.get("uses"):
        # Refuse a composite that could never deploy. The unexpanded spec is
        # what is stored — the copy happens at deploy — but the risk shown is
        # the composite's, since that is what would ship today.
        from ..uses import UseError, expand_uses
        try:
            composite = parse_and_validate(expand_uses(yaml_source, definition.owner))
        except UseError as exc:
            raise SpecError(str(exc)) from exc
        spec["risk"] = composite["risk"]
    definition.yaml_source = yaml_source
    definition.parsed_spec = spec
    definition.name = spec["name"]
    definition.description = spec["description"]
    definition.relevance = spec["relevance"]
    definition.risk_level = spec["risk"]
    # A task forked from the catalog keeps the catalog's identity for it, so a
    # playbook that references it by uid still resolves after the operator
    # renames their copy.
    if spec.get("uid"):
        definition.community_uid = spec["uid"]


# ---------------------------------------------------------------------------
# Community content — sourced from the public GitHub repo
# ---------------------------------------------------------------------------
# The Community tab lists YAML from three directories of that repo: tasks/,
# playbooks/ and automations/. Fetched server-side (which avoids per-browser
# GitHub rate limits) and cached for 10 minutes per kind. Submissions still
# flow the other way as a GitHub PR opened from an editor — see
# openCommunitySubmit() in vigil-tasks.js.
VIGIL_COMMUNITY_REPO = "Susquehanna-Syntax/Vigil-Approved-Scripts"
_COMMUNITY_CACHE_KEY = "vigil_community_templates"
_COMMUNITY_CACHE_TTL = 600  # seconds
_COMMUNITY_MAX_TEMPLATES = 50

#: The directories the repo publishes, and how to read a file from each into
#: the card fields the grid renders. Adding a fourth content type is a matter
#: of adding a parser here and a sub-tab in the UI.
COMMUNITY_KINDS = ("tasks", "playbooks", "automations")


def _card_for_task(text: str) -> dict:
    spec = parse_and_validate(text)
    return {
        "uid": spec.get("uid", ""),
        "name": spec["name"],
        "description": spec.get("description", ""),
        "author": spec.get("author", ""),
        # `relevance:` is free text folded into the description at parse
        # time; the task entry has nothing left to show here. Playbook and
        # automation cards keep their own display summaries.
        "relevance": "",
        "risk_level": spec.get("risk", "standard"),
        "parsed_spec": spec,
        "requires": [],
    }


def _card_for_playbook(text: str) -> dict:
    from apps.playbooks.community_yaml import parse as parse_playbook

    parsed = parse_playbook(text)
    steps = parsed["steps"]
    return {
        "uid": parsed["uid"],
        "name": parsed["name"],
        "description": parsed["description"],
        "author": parsed["author"],
        "relevance": ", ".join(parsed["target_tags"]),
        "risk_level": "high" if parsed["allow_high_risk"] else "standard",
        "step_count": len(steps),
        "summary": f"{len(steps)} step{'' if len(steps) == 1 else 's'}",
        # Structured, not bare slugs: the UI shows what a fork would pull in,
        # and the cascade needs the uid to decide whether it is already held.
        "requires": [{"kind": "tasks", "slug": step["task"],
                      "uid": step.get("uid", "")} for step in steps],
    }


def _card_for_automation(text: str) -> dict:
    from apps.automations.community_yaml import parse as parse_automation

    parsed = parse_automation(text)
    if parsed["trigger"] == "event":
        when = f"on {parsed['event']}"
    else:
        cron = parsed["cron"]
        when = ("on schedule " + " ".join(
            cron[f] for f in ("minute", "hour", "dom", "month", "dow")))
    return {
        "uid": parsed["uid"],
        "name": parsed["name"],
        "description": parsed["description"],
        "author": parsed["author"],
        # `relevance` is the trigger and `summary` is the action. They are
        # rendered as separate chips, so putting the trigger in both prints it
        # twice on the card.
        "relevance": when,
        "risk_level": "standard",
        "summary": f"runs {parsed['action_kind']} {parsed['slug']}",
        "requires": [{
            "kind": "playbooks" if parsed["action_kind"] == "playbook" else "tasks",
            "slug": parsed["slug"],
            "uid": parsed.get("action_uid", ""),
        }],
    }


_COMMUNITY_PARSERS = {
    "tasks": _card_for_task,
    "playbooks": _card_for_playbook,
    "automations": _card_for_automation,
}


# ── Editor templates (bundled with the server) ───────────────────────────────
#
# The editor's "Start from a template" list. Each template is a real task YAML
# file under ``editor_templates/`` and ``index.yaml`` is the order and the
# labels, so one file per template keeps them readable and diffable, and a test
# can prove each one parses. Read once per process and kept in memory: they are
# static files, and the editor asks for them on every page it opens.

_EDITOR_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "editor_templates"  # apps/tasks/
_editor_templates: list[dict] | None = None


def _load_editor_templates() -> list[dict]:
    global _editor_templates
    if _editor_templates is not None:
        return _editor_templates
    import yaml as _yaml

    index = _yaml.safe_load(
        (_EDITOR_TEMPLATE_DIR / "index.yaml").read_text()) or []
    loaded: list[dict] = []
    for entry in index:
        path = _EDITOR_TEMPLATE_DIR / str(entry.get("file", ""))
        if not path.is_file():
            continue
        loaded.append({
            "id": str(entry.get("id", path.stem)),
            "label": str(entry.get("label", path.stem)),
            "group": str(entry.get("group", "")),
            "summary": str(entry.get("summary", "")),
            "yaml": path.read_text(),
        })
    _editor_templates = loaded
    return loaded


# ── Deploy ───────────────────────────────────────────────────────────────────


def _verify_confirmation(user, payload) -> str | None:
    """2FA gate for task deploys — delegates to the shared TOTP helper.

    Returns an error message if the TOTP challenge fails, else None.
    """
    from apps.accounts.totp import require_totp_confirmation

    return require_totp_confirmation(user, payload)


_HUNT_HOST_PENDING = ("pending", "dispatched", "executing")
_HUNT_HOST_ERROR = ("failed", "rejected", "expired")


def _hunt_host_state(task, match_count):
    if task.state in _HUNT_HOST_PENDING:
        return "pending"
    if task.state == Task.State.NOT_APPLICABLE:
        return "not_applicable"
    # Expired without ever being dispatched: the hunt stayed open past its
    # stays_open and the host never checked in — "did not report", not an error.
    if task.state == Task.State.EXPIRED and task.dispatched_at is None:
        return "did_not_report"
    if task.state in _HUNT_HOST_ERROR:
        return "error"
    return "matched" if match_count > 0 else "not_matched"


# ── Staged rollouts ────────────────────────────────────────────────────────────
# Halting and resuming a rollout are state-changing operations on fleet-wide
# patching, so they follow the same gates as manual deploys: TOTP confirmation
# for the operator (plus admin for halt — stopping a rollout mid-flight is the
# heavier decision).


def _rollout_wave_progress(rollout: PatchRollout, user=None) -> list:
    """Per-wave counts for the serializer: every wave in order, annotated with
    this rollout's dispatched tasks for that wave.

    Wave display status:
      * ``passed``    — wave fully reported, failure rate within threshold
      * ``failed``    — wave fully reported, failure rate over threshold
      * ``running``   — current wave, tasks still in flight
      * ``validating``   — current wave, passed, validation window not yet elapsed
      * ``halted``    — the rollout halted on this wave
      * ``pending``   — not yet reached by the rollout
    """
    from ..models import PatchWave

    SUCCESS_STATES = (Task.State.COMPLETED, Task.State.SKIPPED)
    from ..rollout import _waves_in_group

    # A rollout walks one ladder, its site's or the global one (S1 04h): show
    # those waves, not every ladder's. Disabled rungs stay listed.
    waves = PatchWave.objects.order_by("order", "id")
    if (rollout.wave_group_tag or "").strip():
        waves = waves.filter(group_tag_rows__key=rollout.wave_group_tag.strip().lower()).distinct()
    walked = _waves_in_group(rollout.wave_group_tag, rollout.site_id)
    if walked.filter(site_id__isnull=False).exists():
        waves = waves.filter(site_id=rollout.site_id)
    else:
        waves = waves.filter(site_id__isnull=True)
    waves = list(waves)
    per_wave: dict = {}
    from ..authz import trim_run, visible_runs, visible_tasks

    runs = rollout.runs.all()
    if user is not None:
        runs = [trim_run(run, user) for run in visible_runs(runs, user)]
    for run in runs:
        if run.wave is None:
            continue
        tasks = run.tasks.all() if user is None else visible_tasks(run.tasks.all(), user)
        total = tasks.count()
        done = tasks.filter(state__in=SUCCESS_STATES).count()
        failed = tasks.filter(state__in=FAILURE_STATES).count()
        prev = per_wave.setdefault(
            run.wave.id, {"hosts": 0, "tasks_total": 0, "tasks_done": 0, "tasks_failed": 0}
        )
        prev["hosts"] = max(prev["hosts"], run.host_count)
        prev["tasks_total"] += total
        prev["tasks_done"] += done
        prev["tasks_failed"] += failed

    out = []
    for wave in waves:
        counts = per_wave.get(
            wave.id, {"hosts": 0, "tasks_total": 0, "tasks_done": 0, "tasks_failed": 0}
        )
        total = counts["tasks_total"]
        reported = counts["tasks_done"] + counts["tasks_failed"]
        pct = (counts["tasks_failed"] * 100) / reported if reported else 0.0
        is_current = rollout.current_wave_id == wave.id
        if not wave.enabled:
            status = "pending"
        elif is_current and rollout.state == PatchRollout.State.HALTED:
            status = "halted"
        elif is_current and rollout.state == PatchRollout.State.VALIDATING:
            status = "validating"
        elif is_current and rollout.state == PatchRollout.State.RUNNING and total and reported < total:
            status = "running"
        elif total and reported == total:
            status = "failed" if pct > rollout.failure_threshold_pct else "passed"
        elif total:
            status = "running"
        else:
            status = "pending"
        out.append({
            "id": str(wave.id),
            "name": wave.name,
            "order": wave.order,
            "tags": wave.tags or [],
            "validation_hours": wave.validation_hours,
            "enabled": wave.enabled,
            **counts,
            "status": status,
        })
    return out


class _RolloutDetailSerializer(PatchRolloutSerializer):
    """Pass ``context={"user": ...}``: the wave counts are then the caller's
    hosts only (apps/tasks/authz.py, Rollouts)."""

    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["waves"] = _rollout_wave_progress(obj, user=self.context.get("user"))
        return data


def _rollout_response(rollout: PatchRollout, user):
    return _RolloutDetailSerializer(
        PatchRollout.objects.select_related(
            "definition", "current_wave", "created_by", "halted_by", "resumed_by",
        ).get(pk=rollout.pk),
        context={"user": user},
    ).data


# ── Wave management ─────────────────────────────────────────────────────────
#
# Waves are edited like playbooks and automations: list, create, edit, delete.
# Reads are open to any authenticated user so the Deployments page can render;
# writes are admin-only, because changing a wave's tags changes which machines
# the next rollout touches.


def _wave_order_conflict(order, group_tags, exclude_pk=None, site_id=None):
    """The wave already sitting at *order* on any of these ladders, or None.

    Order used to be unique in the database. It cannot be any more: two ladders
    each want their own wave 1, and a wave can sit on several. The invariant
    that still matters is per-ladder, so it is checked here — two rungs at the
    same height on one ladder have no defined walking order.
    """
    from ..models import PatchWave

    wanted = {str(t).strip().lower() for t in (group_tags or []) if str(t).strip()}
    # Ladders are per site (S1 04h): West's wave 1 and Lab's do not collide.
    candidates = PatchWave.objects.filter(order=order, site_id=site_id)
    if exclude_pk is not None:
        candidates = candidates.exclude(pk=exclude_pk)
    for other in candidates.prefetch_related("group_tag_rows"):
        theirs = {t.key for t in other.group_tag_rows.all()}
        # Two ungrouped waves share the one implicit ladder.
        if (wanted & theirs) or (not wanted and not theirs):
            return other
    return None


def _visible_waves(user):
    """Global waves and those of the sites *user* can see (S1 04h)."""
    from ..models import PatchWave
    return scoping.filter_by_site_field(PatchWave.objects.all(), user, field="site_id")


# ---------------------------------------------------------------------------
# Cascade fork — bring an item and everything it needs across in one act
# ---------------------------------------------------------------------------
#
# A playbook is a sequence of tasks and an automation runs a task or a playbook,
# so forking one of those alone lands you with something that cannot run. The
# first version refused and listed what was missing, which was honest but left
# the operator doing the resolution by hand — reading slugs off an error, then
# hunting for each one in another tab.
#
# This resolves the whole graph server-side and forks only what is genuinely
# absent. "Absent" is decided by community uid first and name second, so
# re-forking is idempotent rather than a way to accumulate duplicates.




# Every name here, private ones included, for the other modules' star imports.
__all__ = ["ACTION_REGISTRY", "AllowAny", "COMMUNITY_KINDS", "Count", "F", "FAILURE_STATES", "HIGH_RISK_HOLD_SECONDS", "Host", "Http404", "HuntMatch", "IntegrityError", "IsAdmin", "IsAuthenticated", "PatchRollout", "PatchRolloutSerializer", "Path", "Response", "SpecError", "Task", "TaskDefinition", "TaskDefinitionSerializer", "TaskRun", "TaskRunSerializer", "TaskRunSummarySerializer", "TaskSerializer", "VIGIL_COMMUNITY_REPO", "_BRANCH_STATUS", "_COMMUNITY_CACHE_KEY", "_COMMUNITY_CACHE_TTL", "_COMMUNITY_MAX_TEMPLATES", "_COMMUNITY_PARSERS", "_EDITOR_TEMPLATE_DIR", "_FAILURE_LIKE", "_HUNT_FIELD_MAX", "_HUNT_HOST_ERROR", "_HUNT_HOST_PENDING", "_HUNT_KEY_MAX", "_HUNT_MATCHES_PER_STEP", "_HUNT_MATCHES_PER_TASK", "_MAX_RESULT_BYTES", "_MAX_RESULT_STR", "_MAX_STEPS", "_RolloutDetailSerializer", "_STEP_STATUSES", "_TERMINAL_STATES", "_TRIVY_OUTPUT_KEEP", "_UPDATABLE_STATES", "_card_for_automation", "_card_for_playbook", "_card_for_task", "_deploy_params", "_editor_templates", "_emit_hunt_text_requested", "_hunt_host_state", "_hunt_text_step_ids", "_load_editor_templates", "_rollout_response", "_rollout_wave_progress", "_save_definition_from_yaml", "_user_can_see", "_validate_on_failure", "_validate_schedule", "_validate_success_criteria", "_verify_confirmation", "_visible_waves", "_wave_order_conflict", "api_view", "authenticate_agent", "get_object_or_404", "halt_batch", "halt_rollout", "json", "math", "now", "parse_and_validate", "permission_classes", "resolve_inputs", "resume_rollout", "scoping", "secrets", "start_rollout", "start_rollout_batch", "status", "timedelta", "transaction", "uuid"]
