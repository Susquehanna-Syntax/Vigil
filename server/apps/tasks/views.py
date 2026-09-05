import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.hosts.authentication import authenticate_agent
from apps.hosts.models import Host

from .models import PatchRollout, Task, TaskDefinition, TaskRun
from .rollout_serializers import PatchRolloutSerializer
from .rollout import (
    FAILURE_STATES,
    halt_rollout,
    resume_rollout,
    start_rollout,
)
from .serializers import (
    TaskDefinitionSerializer,
    TaskRunSerializer,
    TaskRunSummarySerializer,
    TaskSerializer,
)
from .spec import (
    ACTION_REGISTRY,
    SpecError,
    _validate_on_failure,
    _validate_schedule,
    _validate_success_criteria,
    parse_and_validate,
    resolve_inputs,
)

_TERMINAL_STATES = {
    Task.State.COMPLETED, Task.State.FAILED,
    Task.State.REJECTED, Task.State.SKIPPED,
}
_UPDATABLE_STATES = {Task.State.DISPATCHED, Task.State.EXECUTING}

#: Characters kept at each end of a Trivy scan output once it has been
#: successfully ingested. A full report runs to megabytes and buries every
#: step that ran after it in the run-detail modal; the findings themselves
#: now live in VulnFinding rows, so the blob is redundant. Head and tail are
#: preserved because in a multi-step run they are other steps' output.
_TRIVY_OUTPUT_KEEP = 2000

# ── Agent-facing: task result ────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([AllowAny])
def task_result(request):
    host, err = authenticate_agent(request)
    if err:
        return err

    task_id = request.data.get("task_id")
    new_state = request.data.get("state")
    output = request.data.get("output", "")

    if not task_id or not new_state:
        return Response({"error": "task_id and state are required"}, status=400)

    if new_state not in _TERMINAL_STATES:
        return Response(
            {"error": f"state must be one of: {', '.join(sorted(_TERMINAL_STATES))}"},
            status=400,
        )

    try:
        task = Task.objects.get(pk=task_id, host=host)
    except Task.DoesNotExist:
        return Response({"error": "Task not found"}, status=404)

    if task.state not in _UPDATABLE_STATES:
        return Response(
            {"error": f"Task is in state '{task.state}' and cannot be updated"},
            status=400,
        )

    with transaction.atomic():
        task.state = new_state
        task.result_output = output
        task.completed_at = now()
        task.save()

        if task.run_id:
            _advance_run_sequence(task)

        # If the parent definition is flagged ``collect:``, capture this
        # successful run's output into the host's inventory custom columns.
        if new_state == Task.State.COMPLETED:
            _maybe_capture_inventory_column(task, output)
            _maybe_apply_tags(task)
            _maybe_apply_playbook_completion_tag(task)
            _maybe_request_nessus_scan(task)
            _maybe_ingest_trivy_report(task, output)
            _maybe_ingest_firewall_rules(task, output)

    return Response(TaskSerializer(task).data)


def _tag_steps(task: Task, actions: tuple[str, ...]) -> list[dict]:
    """Every step of *task* whose action is one of *actions*.

    Multi-step tasks arrive as ``action == "_script"`` with the individual
    steps in ``params.steps``; single-action tasks carry it at the top level.
    Unlike the scan marker, every match counts — a definition may legitimately
    add two tags in two steps.
    """
    found = []
    if task.action in actions:
        found.append(task.params or {})
    for step in (task.params or {}).get("steps") or []:
        if isinstance(step, dict) and step.get("action") in actions:
            found.append(step)
    return found


def _named_tags(step: dict) -> list[str]:
    """Tags named by a tag step, as a list of non-empty strings."""
    params = step.get("params") if isinstance(step.get("params"), dict) else step
    raw = (params or {}).get("tags", "")
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(t).strip() for t in (raw or []) if str(t).strip()]


def _maybe_apply_playbook_completion_tag(task: Task) -> None:
    """Tag the host once the playbook that produced this task has succeeded.

    The tag is what stops an auto-enrolling playbook dispatching to the same
    host on every reconcile pass, so it is written from the run's own playbook
    rather than anything the agent reported.

    Never raises: a task result must be recordable even if tagging fails. A
    failure here costs a repeat dispatch on the next pass, not a lost result.
    """
    import logging

    logger = logging.getLogger(__name__)

    run = task.run
    if run is None or run.playbook_id is None:
        return
    tag = (run.playbook.completion_tag or "").strip()
    if not tag or tag.startswith("agent:"):
        return

    try:
        host = task.host
        tags = list(host.tags or [])
        if any(str(t).strip().lower() == tag.lower() for t in tags):
            return
        host.tags = tags + [tag]
        # Not update_fields=["tags"]: Host.save() syncs the tag rows the
        # matcher actually reads, and it only runs on a full save.
        host.save()
    except Exception:
        logger.exception("completion tag %r failed for host %s", tag, task.host_id)


def _maybe_apply_tags(task: Task) -> None:
    """Apply add_tag / remove_tag steps from a completed task.

    The tags come from the task the server stored and signed, never from the
    agent's reported output — so a compromised agent can at most claim success
    on a tag an operator already wrote into the definition. It cannot choose
    one. That is what lets these be low-risk actions while ``agent:`` stays
    reserved for tags an agent asserts about itself at check-in.

    Never raises: a task result must be recordable even if tagging fails.
    """
    import logging

    logger = logging.getLogger(__name__)

    add_steps = _tag_steps(task, ("add_tag",))
    remove_steps = _tag_steps(task, ("remove_tag",))
    if not add_steps and not remove_steps:
        return

    try:
        host = task.host
        tags = list(host.tags or [])

        for step in add_steps:
            for tag in _named_tags(step):
                # Reserved so a rogue agent cannot impersonate an
                # operator-set tag. Refused at definition-save time too;
                # this writes straight to the host, so it re-checks.
                if tag.startswith("agent:") or tag in tags:
                    continue
                tags.append(tag)

        for step in remove_steps:
            doomed = {t for t in _named_tags(step) if not t.startswith("agent:")}
            tags = [t for t in tags if t not in doomed]

        if tags != list(host.tags or []):
            host.tags = tags
            host.save(update_fields=["tags"])
            logger.info("task %s retagged %s: %s", task.id, host.hostname, tags)
    except Exception:
        logger.exception("task %s: could not apply tag changes", task.id)


def _maybe_request_nessus_scan(task: Task) -> None:
    """If the completed task asked for a network scan, queue one.

    Handles both the engine-specific ``request_nessus_scan`` action
    (always creates a Nessus VulnScan) and the engine-agnostic
    ``request_network_scan`` (consults ``params.engine`` on the
    matching step, falls back to ``nessus`` for back-compat).

    Multi-step tasks have ``action == "_script"`` and an array of
    individual step actions in ``params.steps``. A single occurrence
    is enough to schedule one scan.
    """
    from apps.vulns.models import VulnScan

    steps = (task.params or {}).get("steps") or []

    matched_step = None
    matched_action = None
    if task.action in ("request_nessus_scan", "request_network_scan"):
        matched_action = task.action
        matched_step = task.params or {}
    else:
        for s in steps:
            if isinstance(s, dict) and s.get("action") in ("request_nessus_scan", "request_network_scan"):
                matched_action = s["action"]
                matched_step = s
                break

    if not matched_action:
        return

    # Pick scanner. request_nessus_scan is always Nessus; the agnostic
    # alias honours params.engine, else falls back to Nessus (which is
    # the only network scanner most installs have configured today).
    engine = "nessus"
    if matched_action == "request_network_scan":
        candidate = (
            (matched_step.get("params") or {}).get("engine")
            if isinstance(matched_step.get("params"), dict)
            else matched_step.get("engine") or "nessus"
        )
        if candidate in (VulnScan.Scanner.NESSUS, VulnScan.Scanner.GREENBONE):
            engine = candidate

    # Throttle: skip if there's already an active scan for this host
    # on this scanner — repeats while one is in flight are noise.
    active = VulnScan.objects.filter(
        host=task.host,
        scanner=engine,
        state__in=[
            VulnScan.State.REQUESTED,
            VulnScan.State.LAUNCHED,
            VulnScan.State.RUNNING,
        ],
    ).exists()
    if active:
        return

    VulnScan.objects.create(
        host=task.host,
        scanner=engine,
        target=task.host.ip_address or "",
        state=VulnScan.State.REQUESTED,
        requested_via_task=True,
    )


def _maybe_ingest_trivy_report(task: Task, output: str) -> None:
    """If the completed task ran a ``run_trivy_scan`` step, ingest its JSON.

    Trivy is agent-local — the agent runs the scan and ships the
    full JSON in the task output. We detect the action via either
    ``task.action`` (single-step) or ``task.params.steps`` (multi-step
    via the ``_script`` wrapper), then hand the output to
    :meth:`TrivyScanner.ingest_report`.

    The outcome — counts on success, the reason on failure — is appended to
    the task's own output so it shows up in run details. It used to go only
    to the worker log, which meant a scan that ingested nothing looked
    exactly like a scan that found nothing.

    Failures still don't propagate: a bad output payload can't be allowed to
    poison the task-result endpoint, and the rest of the completion path must
    finish. But they are no longer invisible.
    """
    import logging
    logger = logging.getLogger(__name__)

    steps = (task.params or {}).get("steps") or []
    has_trivy = (
        task.action == "run_trivy_scan"
        or any(isinstance(s, dict) and s.get("action") == "run_trivy_scan" for s in steps)
    )
    if not has_trivy or not output:
        return

    from apps.vulns.scanners.trivy import ScanIngestError, TrivyScanner

    ingested = False
    try:
        status = TrivyScanner().ingest_report(task.host, output)
        logger.info("Trivy ingest for %s: %s", task.host.hostname, status)
        note = f"[INGEST] {status}"
        ingested = True
    except ScanIngestError as exc:
        # Expected, actionable, and the operator needs to see it: the scan
        # ran but produced something we refused to trust.
        logger.warning("Trivy ingest refused for %s: %s", task.host.hostname, exc)
        note = f"[INGEST FAILED] {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Trivy ingest failed for task %s", task.id)
        note = f"[INGEST FAILED] Unexpected error: {exc}"

    try:
        body = task.result_output or ""
        if ingested and len(body) > _TRIVY_OUTPUT_KEEP * 2:
            # The JSON has been fully consumed into VulnFinding rows, and a
            # multi-megabyte blob in the middle of a run buries the steps
            # that ran after it. Elide the middle rather than the whole
            # thing: in a multi-step run the head and tail are other steps'
            # output, which must survive. On failure nothing is elided —
            # that is exactly when the raw report is needed.
            body = (f"{body[:_TRIVY_OUTPUT_KEEP]}\n"
                    f"[… {len(body) - _TRIVY_OUTPUT_KEEP * 2:,} characters of "
                    f"scan JSON elided after successful ingest …]\n"
                    f"{body[-_TRIVY_OUTPUT_KEEP:]}")
        task.result_output = f"{body}\n{note}".strip()
        task.save(update_fields=["result_output"])
    except Exception:  # noqa: BLE001
        logger.exception("Could not attach ingest status to task %s", task.id)


def _maybe_ingest_firewall_rules(task: Task, output: str) -> None:
    """Store a firewall snapshot if this task read one.

    Detected the same way as the Trivy step: via ``task.action`` for a
    single-step task, or ``task.params.steps`` for a multi-step task run
    through the ``_script`` wrapper. A multi-step task's output is the
    runtime's transcript (``[OK] step: <output>``), not bare JSON, so the
    snapshot object is located within the output rather than assumed to be
    the whole thing.

    Failures never propagate — a bad payload must not poison the task-result
    endpoint, which is how every agent task gets reported — but they are
    attached to the task's own output so a read that stored nothing does not
    look like a host with no rules.

    ``enabled`` and the values inside ``defaults`` can be tri-state /
    ``"unknown"`` (Windows reports ``enabled: null`` when a profile read
    fails) and are stored exactly as sent, never coerced to a bool or
    guessed at — an unknown firewall state must never render as a known one.
    """
    import json as _json
    import logging

    from apps.hosts.models import HostFirewall

    logger = logging.getLogger(__name__)

    steps = (task.params or {}).get("steps") or []
    is_read = (
        task.action == "list_firewall_rules"
        or any(isinstance(s, dict) and s.get("action") == "list_firewall_rules"
               for s in steps)
    )
    if not is_read or not output:
        return

    # A multi-step task returns the runtime's transcript, so find the JSON
    # object rather than assuming the whole output is one. The discriminator
    # requires both "rules" and either "tool" or "supported" -- "rules" alone
    # is a generic word an earlier, unrelated step's JSON output could carry,
    # and latching onto that instead of the real snapshot would silently
    # ingest the wrong data. Every backend (and the no-backend path in
    # executor._list_firewall_rules) always emits "tool" and "supported", so
    # this cannot reject a genuine snapshot.
    decoder = _json.JSONDecoder()
    start = output.find("{")
    data = None
    while start != -1:
        try:
            candidate, _end = decoder.raw_decode(output, start)
        except ValueError:
            start = output.find("{", start + 1)
            continue
        if (isinstance(candidate, dict) and "rules" in candidate
                and ("tool" in candidate or "supported" in candidate)):
            data = candidate
            break
        start = output.find("{", start + 1)

    if data is None:
        logger.warning("Firewall ingest found no snapshot for task %s", task.id)
        note = ("[FIREWALL INGEST FAILED] No firewall snapshot found in the "
                f"step output. First 200 characters were: "
                f"{output[:200].replace(chr(10), ' ')!r}")
    else:
        # This hook runs inside task_result's `with transaction.atomic():`
        # block (views.py ~78-93), alongside the write that already saved
        # the task's COMPLETED state in the same transaction. An exception
        # escaping this block would not just lose the firewall snapshot --
        # it would roll back that state write too, 500 the endpoint every
        # agent uses to report every task, and strand the task. A snapshot
        # is agent-reported JSON, off-contract data is expected (e.g.
        # "enabled": "unknown" instead of true/false/null, which raises a
        # ValidationError inside BooleanField.get_prep_value), so this must
        # never be allowed to raise -- same discipline as the trivy hook
        # this one is modelled on.
        try:
            rules_value = data.get("rules") or []
            if not isinstance(rules_value, list):
                # Off-contract: every real backend emits a list. Reject
                # before writing rather than storing something that isn't a
                # rules list -- a write that partially succeeds and then
                # fails while formatting the note would clobber a
                # previously good snapshot while reporting failure, which
                # is worse than rejecting up front.
                raise ValueError(
                    f"'rules' must be a list, got "
                    f"{type(rules_value).__name__}")
            HostFirewall.objects.update_or_create(
                host=task.host,
                defaults={
                    "tool": data.get("tool") or "",
                    "supported": bool(data.get("supported", True)),
                    # Tri-state: True / False / None (unknown). Passed
                    # through as-is -- coercing None to False would render
                    # an unread host as "disabled", which is the exact
                    # failure this tri-state exists to prevent.
                    "enabled": data.get("enabled"),
                    # Values can be "allow" / "deny" / "unknown"; stored as
                    # sent.
                    "defaults": data.get("defaults") or {},
                    "profiles": data.get("profiles") or [],
                    "rules": rules_value,
                    "unparsed": data.get("unparsed") or [],
                },
            )
            note = f"[FIREWALL] {len(rules_value)} rule(s) recorded"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Firewall ingest failed for task %s", task.id)
            note = f"[FIREWALL INGEST FAILED] {exc}"

    try:
        task.result_output = f"{task.result_output or ''}\n{note}".strip()
        task.save(update_fields=["result_output"])
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not attach firewall ingest status to task %s", task.id)


def _maybe_capture_inventory_column(task: Task, output: str) -> None:
    """Write task output into ``HostInventory.custom_columns`` if applicable.

    Skips silently when the task isn't part of a run, the run has no
    definition, or the definition's parsed_spec lacks a ``collect`` block.
    """
    from apps.hosts.models import HostInventory

    run = task.run
    definition = getattr(run, "definition", None) if run else None
    if not definition:
        return
    collect = (definition.parsed_spec or {}).get("collect") or None
    if not collect or not isinstance(collect, dict):
        return
    column = (collect.get("column") or "").strip()
    if not column:
        return
    parse_mode = collect.get("parse") or "output_line_1"

    text = output or ""
    if parse_mode == "output_line_1":
        # Strip the per-step bracketed prefix our agent reports use, then
        # take the first non-empty line of the task output.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        first = lines[0] if lines else ""
        if first.startswith("[OK]"):
            first = first[4:].lstrip()
            if ":" in first:
                first = first.split(":", 1)[1].strip()
        value = first[:500]
    elif parse_mode == "output_trim":
        value = text.strip()[:500]
    else:  # output_full
        value = text[:2000]

    inv, _ = HostInventory.objects.get_or_create(host=task.host)
    columns = dict(inv.custom_columns or {})
    columns[column] = value
    inv.custom_columns = columns
    inv.save(update_fields=["custom_columns", "updated_at"])


def _advance_run_sequence(finished_task: Task) -> None:
    """After a task in a run finishes, unblock the next step on that host.

    If the finished task failed or was rejected, retry policy is consulted:
    when ``retry_count < max_retries`` the original task is reset to PENDING
    (with a fresh nonce/signature and a ``not_before`` delay) so the agent
    re-executes it on the next eligible checkin. Once retries are exhausted
    or no policy applies, all remaining blocked steps on that host are
    rejected. Finally, the run is collapsed into a terminal state when no
    tasks remain eligible to run.
    """
    run = finished_task.run
    sibling_qs = Task.objects.filter(
        run=run, host=finished_task.host, step_order__gt=finished_task.step_order
    ).order_by("step_order")

    # SKIPPED is treated like COMPLETED for chain-advance purposes — the
    # step elected not to run, but it's not a failure. The next step
    # unblocks normally.
    if finished_task.state in (Task.State.COMPLETED, Task.State.SKIPPED):
        next_step = sibling_qs.filter(state=Task.State.BLOCKED).first()
        if next_step:
            next_step.state = Task.State.PENDING
            next_step.save(update_fields=["state"])
    else:
        # Failure path — try to retry the same step before aborting the chain.
        if (
            finished_task.state == Task.State.FAILED
            and finished_task.retry_count < finished_task.max_retries
        ):
            delay = max(0, int(finished_task.retry_delay_seconds or 0))
            finished_task.retry_count += 1
            finished_task.state = Task.State.PENDING
            finished_task.nonce = secrets.token_hex(32)
            finished_task.signature = ""
            finished_task.dispatched_at = None
            finished_task.completed_at = None
            finished_task.not_before = now() + timedelta(seconds=delay) if delay else None
            prior = (finished_task.result_output or "").rstrip()
            attempt_marker = (
                f"\n[retry {finished_task.retry_count}/{finished_task.max_retries} "
                f"scheduled, waiting {delay}s]"
            )
            finished_task.result_output = (prior + attempt_marker).strip()
            finished_task.save(update_fields=[
                "retry_count", "state", "nonce", "signature",
                "dispatched_at", "completed_at", "not_before", "result_output",
            ])
            # Don't finalize the run — there's still active work pending.
            return

        # No retry remaining — abort the rest of the chain for this host.
        sibling_qs.filter(state=Task.State.BLOCKED).update(
            state=Task.State.REJECTED,
            result_output=f"Aborted: step {finished_task.step_order} did not succeed",
            completed_at=now(),
        )

    _finalize_run_if_done(run)


def _finalize_run_if_done(run: TaskRun) -> None:
    active_states = {
        Task.State.BLOCKED,
        Task.State.PENDING,
        Task.State.DISPATCHED,
        Task.State.EXECUTING,
    }
    if Task.objects.filter(run=run, state__in=active_states).exists():
        return

    states = set(Task.objects.filter(run=run).values_list("state", flat=True))
    if states <= {Task.State.COMPLETED, Task.State.SKIPPED}:
        # Skipped steps are happy outcomes — only-skipped or
        # completed-and-skipped runs are COMPLETED, not PARTIAL.
        run.state = TaskRun.State.COMPLETED
    elif Task.State.COMPLETED in states or Task.State.SKIPPED in states:
        run.state = TaskRun.State.PARTIAL
    else:
        run.state = TaskRun.State.FAILED
    run.finished_at = now()
    run.save(update_fields=["state", "finished_at"])


# ── TaskDefinition CRUD ──────────────────────────────────────────────────────


def _user_can_see(definition: TaskDefinition, user) -> bool:
    return (
        definition.visibility == TaskDefinition.Visibility.COMMUNITY
        or definition.owner_id == user.id
    )


def _save_definition_from_yaml(definition: TaskDefinition, yaml_source: str) -> None:
    spec = parse_and_validate(yaml_source)
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
        "relevance": spec.get("relevance", ""),
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


def _fetch_community_templates(kind: str = "tasks") -> list[dict]:
    """Pull and parse the YAML in one of the community repo's directories.

    Invalid or unparsable files are skipped — the repo gates quality through
    PR review, but a bad merge must not blank the whole tab.
    """
    import requests as _requests

    if kind not in _COMMUNITY_PARSERS:
        raise ValueError(f"unknown community kind {kind!r}")
    to_card = _COMMUNITY_PARSERS[kind]

    listing = _requests.get(
        f"https://api.github.com/repos/{VIGIL_COMMUNITY_REPO}/contents/{kind}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if listing.status_code == 404:
        # Repo empty, or that directory not created yet — a valid "nothing
        # here" state rather than an error.
        return []
    listing.raise_for_status()

    entries = [
        e for e in listing.json()
        if isinstance(e, dict)
        and e.get("type") == "file"
        and e.get("name", "").endswith((".yaml", ".yml"))
        and e.get("download_url")
    ][:_COMMUNITY_MAX_TEMPLATES]

    templates = []
    for entry in entries:
        try:
            raw = _requests.get(entry["download_url"], timeout=10)
            raw.raise_for_status()
            card = to_card(raw.text)
        except Exception:
            continue
        templates.append({
            "kind": kind,
            "filename": entry["name"],
            "html_url": entry.get("html_url", ""),
            "yaml_source": raw.text,
            **card,
        })
    return templates


def community_names_by_slug(kind: str) -> dict[str, str]:
    """Map each community file's slug to the ``name`` inside it.

    A slug in the repo is the **filename**, and the repo does not enforce that
    the filename equals ``slugify(name)`` — ``docker-prune-and-restart-unhealthy.yaml``
    is called "Docker Prune and Restart Unhealthy Container". So resolving a
    reference by slugifying library names alone would refuse to import a
    playbook whose task the operator demonstrably has.

    Reads the same server-side cache the Community tab fills, so the common
    path costs nothing. Returns ``{}`` when the repo is unreachable rather than
    raising: an unresolvable slug is already handled, and a network blip must
    not turn a working import into an error.
    """
    from django.core.cache import cache

    if kind not in COMMUNITY_KINDS:
        return {}
    cached = cache.get(f"{_COMMUNITY_CACHE_KEY}:{kind}")
    if cached is None:
        try:
            cached = _fetch_community_templates(kind)
        except Exception:
            return {}
        cache.set(f"{_COMMUNITY_CACHE_KEY}:{kind}", cached, _COMMUNITY_CACHE_TTL)
    names = {}
    for item in cached:
        stem = item.get("filename", "").rsplit(".", 1)[0]
        if stem and item.get("name"):
            names[stem] = item["name"]
    return names


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def community_templates(request, kind: str = "tasks"):
    """List one kind of community content from the public GitHub repo (cached).

    Cached per kind: the three directories are fetched independently, so a
    slow or empty automations/ does not hold up the tasks tab.
    """
    from django.core.cache import cache

    if kind not in COMMUNITY_KINDS:
        return Response({"error": f"unknown content kind {kind!r}"}, status=404)

    cache_key = f"{_COMMUNITY_CACHE_KEY}:{kind}"
    force = request.query_params.get("refresh") == "1"
    if not force:
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)
    try:
        templates = _fetch_community_templates(kind)
    except Exception:
        return Response(
            {"error": "Community repo unreachable — check the server's internet access"},
            status=502,
        )
    cache.set(cache_key, templates, _COMMUNITY_CACHE_TTL)
    return Response(templates)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def definition_list(request):
    """List a user's definitions, or create a new one from YAML."""
    if request.method == "GET":
        scope = request.query_params.get("scope", "mine")
        if scope == "community":
            qs = TaskDefinition.objects.filter(
                visibility=TaskDefinition.Visibility.COMMUNITY
            )
        else:
            qs = TaskDefinition.objects.filter(owner=request.user)
        qs = qs.select_related("owner").order_by("-updated_at")
        return Response(TaskDefinitionSerializer(qs, many=True).data)

    yaml_source = request.data.get("yaml_source", "")
    # All tasks are created private. Sharing happens through the explicit
    # publish endpoint, which later will gate on community-repo upload.
    definition = TaskDefinition(
        owner=request.user, visibility=TaskDefinition.Visibility.PRIVATE
    )
    try:
        _save_definition_from_yaml(definition, yaml_source)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    definition.save()
    return Response(TaskDefinitionSerializer(definition).data, status=201)


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def definition_detail(request, definition_id):
    """Fetch or update a definition. Definitions can't be deleted — playbooks
    and automations reference them, and a vanished definition would silently
    gut those sequences."""
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(definition, request.user):
        return Response({"error": "Not found"}, status=404)

    if request.method == "GET":
        return Response(TaskDefinitionSerializer(definition).data)

    # Mutations require ownership.
    if definition.owner_id != request.user.id:
        return Response({"error": "You do not own this definition"}, status=403)

    # PUT — update from new YAML source. Visibility is NOT editable here;
    # use the publish/unpublish endpoints.
    yaml_source = request.data.get("yaml_source", "")
    try:
        _save_definition_from_yaml(definition, yaml_source)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    definition.save()
    return Response(TaskDefinitionSerializer(definition).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_validate(request):
    """Validate YAML without persisting — used by the editor's live preview."""
    yaml_source = request.data.get("yaml_source", "")
    try:
        spec = parse_and_validate(yaml_source)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)
    return Response({"ok": True, "parsed_spec": spec})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_fork(request, definition_id):
    """Fork a community template into the current user's library."""
    source = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(source, request.user):
        return Response({"error": "Not found"}, status=404)

    copy = TaskDefinition(
        owner=request.user,
        name=source.name,
        description=source.description,
        relevance=source.relevance,
        risk_level=source.risk_level,
        visibility=TaskDefinition.Visibility.PRIVATE,
        yaml_source=source.yaml_source,
        parsed_spec=source.parsed_spec,
        forked_from=source,
    )
    copy.save()
    return Response(TaskDefinitionSerializer(copy).data, status=201)


# ── Deploy ───────────────────────────────────────────────────────────────────


def _verify_confirmation(user, payload) -> str | None:
    """2FA gate for task deploys — delegates to the shared TOTP helper.

    Returns an error message if the TOTP challenge fails, else None.
    """
    from apps.accounts.totp import require_totp_confirmation

    return require_totp_confirmation(user, payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def definition_deploy(request, definition_id):
    """Deploy a definition across one or more hosts.

    Request body::
        {
          "host_ids": ["<uuid>", "<uuid>"],
          "password": "..."           # or "totp": "123456"
        }
    """
    definition = get_object_or_404(TaskDefinition, pk=definition_id)
    if not _user_can_see(definition, request.user):
        return Response({"error": "Not found"}, status=404)

    # Targeting: either an explicit list of host_ids or a list of tags. When
    # tags are supplied we resolve to the set of online, executable hosts
    # that match ANY of the requested tags (union semantics).
    raw_tags = request.data.get("tags") or []
    host_ids = request.data.get("host_ids") or []
    if raw_tags:
        if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
            return Response({"error": "tags must be a list of strings"}, status=400)
        wanted = {t.strip().lower() for t in raw_tags if t.strip()}
        if not wanted:
            return Response({"error": "tags is empty after normalization"}, status=400)
        candidate_hosts = Host.objects.filter(
            status=Host.Status.ONLINE
        ).exclude(mode=Host.Mode.MONITOR)
        host_ids = [
            str(h.id)
            for h in candidate_hosts
            if any(isinstance(t, str) and t.lower() in wanted for t in (h.tags or []))
        ]
        if not host_ids:
            return Response(
                {"error": f"no eligible hosts match tags: {sorted(wanted)}"},
                status=400,
            )
    if not isinstance(host_ids, list) or not host_ids:
        return Response({"error": "host_ids must be a non-empty list"}, status=400)

    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"error": error}, status=401)

    base_spec = definition.parsed_spec
    raw_inputs = request.data.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        return Response({"error": "inputs must be an object"}, status=400)
    try:
        spec = resolve_inputs(base_spec, raw_inputs)
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    # Per-deploy policy overrides — Schedule / Retry / Success Criteria. The
    # deploy modal sends these from its policy tabs, hydrated from the YAML
    # defaults. Each override is validated using the same validators the YAML
    # parser uses, so an attacker can't smuggle a different schema through.
    try:
        if "schedule" in request.data:
            override = _validate_schedule(request.data.get("schedule"))
            spec["schedule"] = override
        if "on_failure" in request.data:
            override = _validate_on_failure(request.data.get("on_failure"))
            spec["on_failure"] = override
        if "success_criteria" in request.data:
            override = _validate_success_criteria(request.data.get("success_criteria"))
            spec["success_criteria"] = override
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)

    actions = spec.get("actions") or []
    # Inline any `type: playbook` calls — agents only ever receive concrete
    # actions (a playbook reference is a server-side macro, not an agent verb).
    from apps.playbooks.expansion import PlaybookExpandError, expand_actions
    try:
        actions, _expanded_risk = expand_actions(actions)
    except PlaybookExpandError as exc:
        return Response({"error": str(exc)}, status=400)
    if not actions:
        return Response({"error": "definition has no actions"}, status=400)

    hosts = list(Host.objects.filter(id__in=host_ids))
    if len(hosts) != len(host_ids):
        return Response({"error": "One or more hosts not found"}, status=404)

    # target_tags acts as an OR filter: a host is eligible if any of its
    # tags appears in the definition's target_tags. Auto-classified tags
    # (os:linux, pkg:apt, etc.) live alongside user tags in Host.tags so
    # one membership check covers both.
    target_tags = set(spec.get("target_tags") or [])
    for host in hosts:
        if host.status != Host.Status.ONLINE:
            return Response(
                {"error": f"Host {host.hostname} is not online"}, status=400
            )
        if host.mode == Host.Mode.MONITOR:
            return Response(
                {"error": f"Host {host.hostname} is in monitor mode"}, status=400
            )
        if target_tags:
            host_tags = {str(t).lower() for t in (host.tags or [])}
            if host_tags.isdisjoint(target_tags):
                return Response(
                    {
                        "error": (
                            f"Host {host.hostname} doesn't carry any of the "
                            f"required target_tags ({sorted(target_tags)}). "
                            f"Host tags: {sorted(host_tags) or '(none)'}."
                        ),
                    },
                    status=400,
                )

    # Build the steps payload the agent will receive.  The full script is
    # sent as a single signed task per host — the agent validates each
    # action against its own local allowlist, so a compromised server
    # cannot escalate beyond what each agent permits.
    success_criteria = spec.get("success_criteria") or None
    steps_payload = []
    for i, action in enumerate(actions):
        step = {
            "id": action.get("id") or f"step{i + 1}",
            "action": action["type"],
            "params": action.get("params") or {},
        }
        # Optional when: predicate evaluated by the agent at execution
        # time. Empty string means "always run" (back-compat).
        when_expr = action.get("when") or ""
        if when_expr:
            step["when"] = when_expr
        # Per-step timeout override. Omitted rather than sent as null when
        # unset, so the agent simply falls back to its own default.
        if action.get("timeout"):
            step["timeout"] = action["timeout"]
        # Success criteria apply to every step in the script. The agent
        # evaluates these after each step's exit and marks the step failed
        # if criteria are not met (even if the action itself succeeded).
        if success_criteria:
            step["success_criteria"] = success_criteria
        steps_payload.append(step)

    # update_agent replaces the whole agent executable. Stamp the verified
    # SHA-256 of each platform binary into the step so the agent can check the
    # download against a digest carried inside this Ed25519-signed task — a
    # TLS-only transfer is not a strong enough proof for that swap.
    if any(s["action"] == "update_agent" for s in steps_payload):
        from apps.agent_dist.views import all_binary_sha256

        sha_map = all_binary_sha256()
        for s in steps_payload:
            if s["action"] == "update_agent":
                s["params"] = {**(s.get("params") or {}), "binary_sha256": sha_map}

    # Effective risk is the highest risk across all actions.
    from apps.playbooks.expansion import _max_risk as _mr
    risk = _mr(spec.get("risk", "standard"), _expanded_risk)

    # Schedule + retry policy are snapshotted onto each Task so a later edit
    # of the TaskDefinition cannot retroactively change in-flight deploys.
    schedule_snapshot = spec.get("schedule") or {}
    retry_cfg = ((spec.get("on_failure") or {}).get("retry") or {})
    max_retries = int(retry_cfg.get("attempts", 0))
    retry_delay = int(retry_cfg.get("delay_seconds", 0))

    with transaction.atomic():
        run = TaskRun.objects.create(
            definition=definition,
            name_snapshot=definition.name,
            requested_by=request.user,
            host_count=len(hosts),
            step_count=len(actions),
            state=TaskRun.State.RUNNING,
        )

        for host in hosts:
            Task.objects.create(
                host=host,
                requested_by=request.user,
                run=run,
                step_order=0,
                step_label=definition.name,
                action="_script",
                # variables carries the resolved inputs the agent needs to
                # evaluate `when: inputs.x ...`. Without it the agent's
                # predicate context has an empty inputs dict, every
                # comparison against a declared input comes out false, and
                # the gated step is silently skipped on every run (#17).
                # Always sent, even when empty, so "no inputs declared" is
                # distinguishable from "inputs lost in the pipeline".
                params={"steps": steps_payload,
                        "variables": spec.get("resolved_inputs") or {}},
                risk_level=risk,
                state=Task.State.PENDING,
                nonce=secrets.token_hex(32),
                schedule=schedule_snapshot,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay,
            )

    return Response(TaskRunSerializer(run).data, status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def task_history(request):
    """Paginated task history feed for the History tab (polled by the UI).

    Returns the full fleet's task history, newest first, in pages of 50.
    """
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    page_size = 50

    qs = (
        Task.objects.filter(hidden=False)
        .select_related("host", "requested_by")
        .order_by("-created_at")
    )
    total = qs.count()
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
    start = (page - 1) * page_size
    items = list(qs[start:start + page_size])
    return Response({
        "count": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "results": TaskSerializer(items, many=True).data,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def run_history(request):
    """Paginated run history, newest first.

    ``?source=automation,playbook`` narrows to those kinds; omitted means
    every run including manual deploys.
    """
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    page_size = 25

    qs = TaskRun.objects.select_related(
        "automation", "playbook", "requested_by").order_by("-created_at")

    raw = (request.query_params.get("source") or "").strip()
    if raw:
        wanted = [s for s in (v.strip() for v in raw.split(",")) if s]
        valid = [s for s in wanted if s in TaskRun.Source.values]
        if not valid:
            return Response({"detail": f"unknown source {raw!r}"}, status=400)
        qs = qs.filter(source__in=valid)

    total = qs.count()
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        page = pages
    start = (page - 1) * page_size
    return Response({
        "count": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "results": TaskRunSummarySerializer(qs[start:start + page_size], many=True).data,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def run_detail(request, run_id):
    run = get_object_or_404(
        TaskRun.objects.prefetch_related("tasks__host").select_related(
            "definition", "requested_by"
        ),
        pk=run_id,
    )
    return Response(TaskRunSerializer(run).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def task_detail(request, task_id):
    """Single-task fetch. History rows are the audit trail — who ran what,
    where, with what result — and are immutable by design; there is no
    delete."""
    task = get_object_or_404(
        Task.objects.select_related("host", "run"),
        pk=task_id,
    )
    return Response(TaskSerializer(task).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def action_registry(request):
    """Expose the action registry for the editor's autocomplete / validation."""
    return Response(ACTION_REGISTRY)


# ── Staged rollouts ────────────────────────────────────────────────────────────
# Halting and resuming a rollout are state-changing operations on fleet-wide
# patching, so they follow the same gates as manual deploys: TOTP confirmation
# for the operator (plus admin for halt — stopping a rollout mid-flight is the
# heavier decision).


def _rollout_wave_progress(rollout: PatchRollout) -> list:
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
    from .models import PatchWave

    SUCCESS_STATES = (Task.State.COMPLETED, Task.State.SKIPPED)
    waves = list(PatchWave.objects.order_by("order", "id"))
    per_wave: dict = {}
    for run in rollout.runs.all():
        if run.wave is None:
            continue
        total = run.tasks.count()
        done = run.tasks.filter(state__in=SUCCESS_STATES).count()
        failed = run.tasks.filter(state__in=FAILURE_STATES).count()
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
    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["waves"] = _rollout_wave_progress(obj)
        return data


def _rollout_response(rollout: PatchRollout):
    return _RolloutDetailSerializer(
        PatchRollout.objects.select_related(
            "definition", "current_wave", "created_by", "halted_by", "resumed_by",
        ).get(pk=rollout.pk)
    ).data


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def rollout_collection(request):
    """GET — list rollouts. POST — start one from a definition or a playbook.

    TOTP-gated like a manual deploy — it fans the work out across the whole
    fleet, wave by wave.
    """
    if request.method == "POST":
        from apps.playbooks.models import Playbook

        definition_id = request.data.get("definition_id")
        playbook_id = request.data.get("playbook_id")
        if bool(definition_id) == bool(playbook_id):
            return Response(
                {"detail": "supply exactly one of definition_id or playbook_id"},
                status=400,
            )
        definition = playbook = None
        if definition_id:
            definition = get_object_or_404(TaskDefinition, pk=definition_id)
        else:
            playbook = get_object_or_404(Playbook, pk=playbook_id)
        error = _verify_confirmation(request.user, request.data)
        if error:
            return Response({"detail": error}, status=401)

        try:
            rollout = start_rollout(
                definition,
                playbook=playbook,
                user=request.user,
                failure_threshold_pct=int(request.data.get("failure_threshold_pct", 10)),
                min_results_before_halt=int(request.data.get("min_results_before_halt", 3)),
            )
        except (ValueError, TypeError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(_rollout_response(rollout), status=201)

    rollouts = PatchRollout.objects.select_related(
        "definition", "current_wave", "created_by", "halted_by", "resumed_by",
    ).order_by("-created_at")
    raw = (request.query_params.get("state") or "").strip()
    if raw:
        wanted = [s for s in (v.strip() for v in raw.split(",")) if s]
        valid = [s for s in wanted if s in PatchRollout.State.values]
        if not valid:
            return Response({"detail": f"unknown state {raw!r}"}, status=400)
        rollouts = rollouts.filter(state__in=valid)
    data = []
    for r in rollouts:
        data.append(_RolloutDetailSerializer(r).data)
    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def rollout_detail(request, rollout_id):
    rollout = get_object_or_404(
        PatchRollout.objects.select_related(
            "definition", "current_wave", "created_by", "halted_by", "resumed_by",
        ),
        pk=rollout_id,
    )
    return Response(_RolloutDetailSerializer(rollout).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def rollout_halt(request, rollout_id):
    """Stop the rollout now. Admin + TOTP: halting a fleet-wide patch
    mid-flight is the heavier of the two operator calls."""
    rollout = get_object_or_404(PatchRollout, pk=rollout_id)
    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"detail": error}, status=401)
    reason = str(request.data.get("reason") or "").strip()
    try:
        halt_rollout(rollout, user=request.user, reason=reason)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response(_rollout_response(rollout))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def rollout_resume(request, rollout_id):
    """Clear a halt and continue from the same wave. TOTP-gated; records who
    did it. Failed tasks on the wave are re-queued so the gate re-evaluates
    over the whole wave."""
    rollout = get_object_or_404(PatchRollout, pk=rollout_id)
    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"detail": error}, status=401)
    try:
        resume_rollout(rollout, user=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response(_rollout_response(rollout))


# ── Wave management ─────────────────────────────────────────────────────────
#
# Waves are edited like playbooks and automations: list, create, edit, delete.
# Reads are open to any authenticated user so the Deployments page can render;
# writes are admin-only, because changing a wave's tags changes which machines
# the next rollout touches.


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def wave_collection(request):
    """List waves in order, or create one."""
    from .models import PatchWave
    from .rollout_serializers import PatchWaveSerializer

    if request.method == "GET":
        waves = PatchWave.objects.order_by("order", "id")
        return Response(PatchWaveSerializer(waves, many=True).data)

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": "Admin role required."}, status=403)
    serializer = PatchWaveSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)
    try:
        serializer.save()
    except IntegrityError:
        # order is unique — say which number collided rather than surfacing a
        # database error to the operator.
        return Response(
            {"order": [f"Wave {request.data.get('order')} already exists."]},
            status=400,
        )
    return Response(serializer.data, status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def wave_detail(request, wave_id):
    """Read, edit, or delete one wave."""
    from .models import PatchRollout, PatchWave
    from .rollout_serializers import PatchWaveSerializer

    wave = get_object_or_404(PatchWave, pk=wave_id)

    if request.method == "GET":
        return Response(PatchWaveSerializer(wave).data)

    if not IsAdmin().has_permission(request, None):
        return Response({"detail": "Admin role required."}, status=403)

    if request.method == "DELETE":
        # Refuse while a rollout is standing on this wave. Deleting it would
        # null current_wave and strand the rollout with nothing to advance from.
        active = PatchRollout.objects.filter(
            current_wave=wave,
            state__in=[PatchRollout.State.RUNNING, PatchRollout.State.VALIDATING,
                       PatchRollout.State.HALTED],
        ).exists()
        if active:
            return Response(
                {"detail": "A rollout is currently on this wave. Halt or finish it first."},
                status=409,
            )
        wave.delete()
        return Response(status=204)

    serializer = PatchWaveSerializer(wave, data=request.data, partial=True)
    if not serializer.is_valid():
        return Response(serializer.errors, status=400)
    try:
        serializer.save()
    except IntegrityError:
        return Response(
            {"order": [f"Wave {request.data.get('order')} already exists."]},
            status=400,
        )
    return Response(serializer.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def rollout_skip_validation(request, rollout_id):
    """End the current wave's validation window early and advance.

    Admin + TOTP, same as halting: it shortens the safety margin on a
    fleet-wide patch, which is an operator decision worth authenticating.
    """
    from .rollout import skip_validation

    rollout = get_object_or_404(PatchRollout, pk=rollout_id)
    error = _verify_confirmation(request.user, request.data)
    if error:
        return Response({"detail": error}, status=401)
    try:
        skip_validation(rollout, user=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=400)
    rollout.refresh_from_db()
    return Response(_rollout_response(rollout))


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


def _library_index(model, user):
    """Index an operator's existing library by uid and by name."""
    from vigil import scoping

    rows = scoping.filter_by_site(model.objects.all(), user, cascade_global=True)
    by_uid, by_name = {}, {}
    for row in rows:
        if row.community_uid:
            by_uid.setdefault(str(row.community_uid), row)
        by_name.setdefault(row.name.strip().lower(), row)
    return by_uid, by_name


def _already_have(by_uid, by_name, uid, name):
    """The operator's existing copy of a catalog item, if they have one.

    When the catalog file carries a uid, that is the *only* thing consulted.
    Falling back to the name there would undo what uids are for: a task the
    operator wrote themselves that happens to also be called "Install Nginx"
    is a different task, and treating it as the catalog's would silently skip
    the fork and leave a playbook pointing at the wrong steps.

    The name pass exists for catalog files written before uids, which have no
    other identity to offer.
    """
    if uid:
        return by_uid.get(str(uid))
    return by_name.get((name or "").strip().lower())


def _catalog(kind):
    """The catalog for *kind*, indexed by slug and by uid."""
    from django.core.cache import cache

    cached = cache.get(f"{_COMMUNITY_CACHE_KEY}:{kind}")
    if cached is None:
        cached = _fetch_community_templates(kind)
        cache.set(f"{_COMMUNITY_CACHE_KEY}:{kind}", cached, _COMMUNITY_CACHE_TTL)
    by_slug, by_uid = {}, {}
    for item in cached:
        stem = item.get("filename", "").rsplit(".", 1)[0]
        if stem:
            by_slug[stem] = item
        if item.get("uid"):
            by_uid[str(item["uid"])] = item
    return by_slug, by_uid


def _find_in_catalog(by_slug, by_uid, ref):
    """Locate a referenced catalog file from a ``{slug, uid}`` reference."""
    uid = (ref.get("uid") or "").strip()
    if uid and uid in by_uid:
        return by_uid[uid]
    return by_slug.get(ref.get("slug", ""))


def _fork_task_from_catalog(item, user) -> TaskDefinition:
    """Create a library task from one catalog file."""
    definition = TaskDefinition(owner=user,
                                visibility=TaskDefinition.Visibility.PRIVATE)
    _save_definition_from_yaml(definition, item["yaml_source"])
    definition.save()
    return definition


def _plan_fork(kind: str, filename: str, user) -> dict:
    """Work out what forking one catalog item would create.

    Returns the item, the ordered list of dependencies, and — for each — the
    operator's existing copy if they have one. Read-only: the same walk backs
    the preview on the card and the fork itself, so what the card promises is
    what the fork does.
    """
    from apps.playbooks.models import Playbook

    task_by_slug, task_by_uid = _catalog("tasks")
    item = None
    if kind == "tasks":
        item = task_by_slug.get(filename.rsplit(".", 1)[0])
    else:
        by_slug, _ = _catalog(kind)
        item = by_slug.get(filename.rsplit(".", 1)[0])
    if item is None:
        return {"error": f"{filename} is not in the catalog"}

    have_tasks_uid, have_tasks_name = _library_index(TaskDefinition, user)
    have_bl_uid, have_bl_name = _library_index(Playbook, user)

    needs = []
    for ref in item.get("requires") or []:
        if ref["kind"] == "tasks":
            source = _find_in_catalog(task_by_slug, task_by_uid, ref)
            existing = _already_have(have_tasks_uid, have_tasks_name,
                                     ref.get("uid"),
                                     source["name"] if source else ref["slug"])
        else:
            bl_by_slug, bl_by_uid = _catalog("playbooks")
            source = _find_in_catalog(bl_by_slug, bl_by_uid, ref)
            existing = _already_have(have_bl_uid, have_bl_name, ref.get("uid"),
                                     source["name"] if source else ref["slug"])
            # A playbook dependency drags its own tasks along.
            for inner in (source or {}).get("requires") or []:
                inner_src = _find_in_catalog(task_by_slug, task_by_uid, inner)
                needs.append({
                    "kind": "tasks", "slug": inner["slug"],
                    "name": inner_src["name"] if inner_src else inner["slug"],
                    "found": inner_src is not None,
                    "have": bool(_already_have(
                        have_tasks_uid, have_tasks_name, inner.get("uid"),
                        inner_src["name"] if inner_src else inner["slug"])),
                    "source": inner_src,
                })
        needs.append({
            "kind": ref["kind"], "slug": ref["slug"],
            "name": source["name"] if source else ref["slug"],
            "found": source is not None,
            "have": existing is not None,
            "source": source,
        })

    # Dedupe, keeping first occurrence: a playbook can use one task twice, and
    # two steps of an automation's playbook can share one.
    seen, ordered = set(), []
    for need in needs:
        key = (need["kind"], need["slug"])
        if key in seen:
            continue
        seen.add(key)
        ordered.append(need)

    # Whether the operator already holds the item itself, not just its
    # dependencies. Without this the cascade skips every dependency, then tries
    # to import the item anyway and trips the duplicate-name check — which
    # reads as a failure when the honest answer is "you already have this".
    from apps.automations.models import Automation

    model = {"tasks": TaskDefinition, "playbooks": Playbook,
             "automations": Automation}[kind]
    have_uid, have_name = _library_index(model, user)
    mine = _already_have(have_uid, have_name, item.get("uid"), item["name"])
    return {"item": item, "needs": ordered, "have": mine is not None}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def community_fork_plan(request, kind: str, filename: str):
    """What forking this catalog item would create, without creating it."""
    if kind not in COMMUNITY_KINDS:
        return Response({"error": f"unknown content kind {kind!r}"}, status=404)
    try:
        plan = _plan_fork(kind, filename, request.user)
    except Exception:
        return Response({"error": "Community repo unreachable"}, status=502)
    if "error" in plan:
        return Response(plan, status=404)
    return Response({
        "name": plan["item"]["name"],
        "have": plan["have"],
        "needs": [{**{k: n[k] for k in ("kind", "slug", "name", "found", "have")},
                   "uid": (n.get("source") or {}).get("uid", "")}
                  for n in plan["needs"]],
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def community_fork(request, kind: str, filename: str):
    """Fork a catalog item and everything it needs, in dependency order.

    Tasks first, then playbooks, then the item itself — anything already held
    is skipped rather than duplicated. The whole thing is one transaction: a
    half-forked playbook missing its third step is not a state worth leaving
    an operator in.
    """
    from apps.automations.views import automation_from_yaml
    from apps.playbooks.views import playbook_from_yaml

    if kind not in COMMUNITY_KINDS:
        return Response({"error": f"unknown content kind {kind!r}"}, status=404)
    try:
        plan = _plan_fork(kind, filename, request.user)
    except Exception:
        return Response({"error": "Community repo unreachable"}, status=502)
    if "error" in plan:
        return Response(plan, status=404)

    if plan["have"]:
        # Already held. A no-op with a clear answer beats a duplicate-name
        # error, which reads as a failure when nothing is actually wrong.
        return Response({"created": [], "already": True,
                         "name": plan["item"]["name"]})

    missing = [n["slug"] for n in plan["needs"] if not n["found"]]
    if missing:
        return Response(
            {"detail": "The catalog is missing files this references: "
                       + ", ".join(sorted(set(missing)))},
            status=400)

    created = []
    with transaction.atomic():
        # Tasks before playbooks: a playbook import resolves its steps against
        # the library, so its tasks have to be in there first.
        for need in [n for n in plan["needs"] if n["kind"] == "tasks"]:
            if need["have"]:
                continue
            _fork_task_from_catalog(need["source"], request.user)
            created.append({"kind": "tasks", "name": need["name"]})

        for need in [n for n in plan["needs"] if n["kind"] == "playbooks"]:
            if need["have"]:
                continue
            sub = _import_via(playbook_from_yaml, request, need["source"])
            if sub.status_code >= 400:
                transaction.set_rollback(True)
                return sub
            created.append({"kind": "playbooks", "name": need["name"]})

        item = plan["item"]
        if kind == "tasks":
            definition = _fork_task_from_catalog(item, request.user)
            created.append({"kind": "tasks", "name": definition.name})
        else:
            view = playbook_from_yaml if kind == "playbooks" else automation_from_yaml
            result = _import_via(view, request, item)
            if result.status_code >= 400:
                transaction.set_rollback(True)
                return result
            created.append({"kind": kind, "name": item["name"]})

    return Response({"created": created}, status=status.HTTP_201_CREATED)


def _import_via(view, request, item):
    """Run one of the YAML import views against a catalog file.

    Reusing the view rather than its internals is deliberate: the eligibility
    rules and the TOTP gate on ``allow_high_risk`` live there, and a cascade
    fork must not be a way past either.
    """
    from rest_framework.test import APIRequestFactory, force_authenticate

    factory = APIRequestFactory()
    sub = factory.post("/", {"yaml": item["yaml_source"]}, format="json")
    force_authenticate(sub, user=request.user)
    return view(sub)
