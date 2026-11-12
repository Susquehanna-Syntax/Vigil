"""apps.tasks.views: shared helpers (ingest), moved unchanged."""
from ._common import *  # noqa: F401,F403


def _clean_step_results(raw):
    """Sanitise agent-reported step results before they are stored.

    The agent is untrusted here: keep at most ``_MAX_STEPS`` entries, each a
    dict with a string ``id`` (<= 60 chars), a known ``status``, and a dict
    ``result`` holding only str/bool/int/float values (strings cut to
    4096). Anything that would serialise over ``_MAX_RESULT_BYTES`` is
    dropped wholesale.
    """
    if not isinstance(raw, list):
        return {}
    out = []
    for entry in raw[:_MAX_STEPS]:
        if not isinstance(entry, dict):
            continue
        step_id = entry.get("id")
        status = entry.get("status")
        result = entry.get("result")
        if (
            not isinstance(step_id, str)
            or len(step_id) > 60
            or status not in _STEP_STATUSES
            or not isinstance(result, dict)
        ):
            continue
        clean_result = {}
        for key, value in result.items():
            if not isinstance(key, str) or len(key) > 60:
                continue
            if isinstance(value, str):
                value = value[:_MAX_RESULT_STR]
            elif not isinstance(value, (bool, int, float)):
                continue
            elif isinstance(value, float) and not math.isfinite(value):
                # NaN/Infinity survive JSON parsing but PostgreSQL's jsonb
                # refuses them, which would fail the whole result save.
                continue
            clean_result[key] = value
        out.append({"id": step_id, "status": status, "result": clean_result})
    if len(json.dumps(out)) > _MAX_RESULT_BYTES:
        return {"steps": [], "dropped": "step results exceeded 64 KB"}
    return {"steps": out}


def _clean_hunt_match(match):
    """(evidence_type, field dict) for one agent-reported match, or None.

    The agent is untrusted: the match must be a dict whose ``evidence_type``
    is a string (<= 40 chars); every other key must be a string (<= 60
    chars) with a str (cut to 1000), int, finite float, bool or None value.
    Nested structures and anything else are dropped from the stored row.
    """
    if not isinstance(match, dict):
        return None
    evidence_type = match.get("evidence_type")
    if not isinstance(evidence_type, str) or not evidence_type or len(evidence_type) > 40:
        return None
    data = {}
    for key, value in match.items():
        if key == "evidence_type":
            continue
        if not isinstance(key, str) or len(key) > _HUNT_KEY_MAX:
            continue
        if isinstance(value, str):
            value = value[:_HUNT_FIELD_MAX]
        elif isinstance(value, (bool, int)) or value is None:
            pass
        elif isinstance(value, float):
            if not math.isfinite(value):
                continue
        else:
            continue
        data[key] = value
    return evidence_type, data


def _ingest_hunt_matches(task, raw_steps):
    """Store hunt matches reported with a task result.

    Replaces (delete then bulk_create) the task's previous rows so a
    re-report is idempotent. The action for each match is looked up from the
    task's own signed ``params["steps"]`` — never from the agent's report —
    and a step id the task does not contain is skipped. Per-step
    ``truncated`` / ``timed_out`` / ``duration`` land in
    ``result_data["hunts"][step_id]`` (simple values only).

    ``task.result_data`` must already hold the steps for this report —
    ``task_result`` assigns and saves it before calling here, so the
    ``update_fields=["result_data"]`` save below cannot clobber the steps.
    """
    if not isinstance(raw_steps, list):
        return

    hunts = {}

    signed_actions = {}
    for step in (task.params or {}).get("steps") or []:
        if (isinstance(step, dict)
                and isinstance(step.get("id"), str)
                and isinstance(step.get("action"), str)):
            signed_actions[step["id"]] = step["action"]
    # Probes of the signed ``relevant:`` tree are signed evidence too: the
    # agent runs them before any step and reports them as steps, so their
    # matches are stored like any other hunt step's — with the action the
    # server itself signed, never the report's.
    relevant = (task.params or {}).get("relevant")
    if isinstance(relevant, dict):
        probe_actions = {}

        def _collect(node) -> None:
            for item in node.get("items") or []:
                if not isinstance(item, dict):
                    continue
                if "probe" in item:
                    probe = item.get("probe")
                    if (isinstance(probe, dict)
                            and isinstance(probe.get("id"), str)
                            and isinstance(probe.get("type"), str)):
                        probe_actions[probe["id"]] = probe["type"]
                else:
                    _collect(item)

        _collect(relevant)
        for probe_id, probe_type in probe_actions.items():
            signed_actions.setdefault(probe_id, probe_type)
    # A detection task's boost: probes (M10) are signed the same way.
    for probe in (task.params or {}).get("boost") or []:
        if (isinstance(probe, dict) and isinstance(probe.get("id"), str)
                and isinstance(probe.get("type"), str)):
            signed_actions.setdefault(probe["id"], probe["type"])

    rows = []
    task_remaining = _HUNT_MATCHES_PER_TASK
    for entry in raw_steps:
        if not isinstance(entry, dict):
            continue
        hunt = entry.get("hunt")
        if not isinstance(hunt, dict) or not isinstance(hunt.get("matches"), list):
            continue
        step_id = entry.get("id")
        if not isinstance(step_id, str) or not step_id or len(step_id) > 60:
            continue
        # The action comes from the task's own signed steps, never the report;
        # a step id the task does not contain is not ours to store.
        action = signed_actions.get(step_id)
        if action is None or len(action) > 64:
            continue

        kept = [c for c in (_clean_hunt_match(m) for m in hunt["matches"]) if c]
        stored = kept[:min(_HUNT_MATCHES_PER_STEP, task_remaining)]
        task_remaining -= len(stored)
        rows.extend(
            HuntMatch(task=task, run_id=task.run_id, host=task.host,
                      step_id=step_id, action=action,
                      evidence_type=evidence_type, data=data)
            for evidence_type, data in stored)
        duration = hunt.get("duration")
        hunts[step_id] = {
            # Cut here counts as truncated too: the view must not claim
            # it shows everything the host found.
            "truncated": bool(hunt.get("truncated")) or len(stored) < len(kept),
            "timed_out": bool(hunt.get("timed_out")),
            "duration": duration if isinstance(duration, (int, float))
            and not isinstance(duration, bool) and math.isfinite(duration) else None,
        }

    if not hunts:
        return

    # Replace, so a re-report (including one that now finds nothing) is idempotent.
    HuntMatch.objects.filter(task=task).delete()
    if rows:
        HuntMatch.objects.bulk_create(rows)

    data = task.result_data if isinstance(task.result_data, dict) else {}
    data["hunts"] = hunts
    task.result_data = data
    task.save(update_fields=["result_data"])


def _maybe_run_alert_actions(task: Task) -> None:
    """Carry out the raise_alert / resolve_alert / send_notification steps of
    a task that reached a terminal state.

    The work lives with the alerts app. Never raises: a task result must be
    recordable even if alerting fails, which is why the import is here too —
    a broken alerts app must not cost the result.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from apps.alerts.task_actions import apply
        apply(task)
    except Exception:
        logger.exception("alert task actions failed on task %s", task.pk)
    try:
        from apps.jackil.task_actions import apply as apply_jackil
        apply_jackil(task)
    except Exception:
        logger.exception("jackil task actions failed on task %s", task.pk)


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
    """Tag the host once the playbook chain that produced this task has
    finished on the host without failing.

    Phase 08a: a playbook runs one task per step, so the tag lands only
    when the host's tasks in that run are all terminal and none is
    FAILED / REJECTED / EXPIRED — NOT_APPLICABLE and SKIPPED count as
    done (a chain that stopped as not applicable is finished, and must
    not be redispatched by every reconcile pass).

    The tag is what stops an auto-enrolling playbook dispatching to the
    same host on every reconcile pass, so it is written from the run's
    own playbook rather than anything the agent reported.

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

    siblings = list(Task.objects.filter(run=run, host=task.host)
                    .values_list("state", "on_failure"))
    if not {state for state, _ in siblings}.issubset(_TERMINAL_STATES):
        return
    # A failure the playbook handles (on_failure: continue) does not block it.
    if any(state in _FAILURE_LIKE and on_failure != "continue"
           for state, on_failure in siblings):
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


def _maybe_ingest_docker_usage(task: Task, output: str) -> None:
    """Store a ``docker_disk_usage`` reading (14.3) as the host's HostDockerUsage.

    Like the firewall hook this runs inside task_result's transaction and
    must never raise: the output is agent-reported JSON, found by its
    ``"kind": "docker_disk_usage"`` marker in a possibly multi-step transcript.
    """
    import json as _json
    import logging

    from django.utils.timezone import now as _now

    from apps.hosts.models import HostDockerUsage

    steps = (task.params or {}).get("steps") or []
    if not (task.action == "docker_disk_usage"
            or any(isinstance(st, dict) and st.get("action") == "docker_disk_usage" for st in steps)):
        return
    if not output:
        return
    decoder = _json.JSONDecoder()
    start, data = output.find("{"), None
    while start != -1:
        try:
            candidate, _end = decoder.raw_decode(output, start)
        except ValueError:
            start = output.find("{", start + 1)
            continue
        if isinstance(candidate, dict) and candidate.get("kind") == "docker_disk_usage":
            data = candidate
            break
        start = output.find("{", start + 1)
    if data is None:
        return

    def n(key):
        value = data.get(key)
        return value if isinstance(value, int) and value >= 0 else 0

    try:
        volume_sizes = {str(v.get("name"))[:255]: (v.get("size") if isinstance(v.get("size"), int) else None)
                        for v in (data.get("volumes") or [])[:2000] if isinstance(v, dict) and v.get("name")}
        image_containers = {str(i.get("id"))[:80]: int(i.get("containers") or 0)
                            for i in (data.get("images") or [])[:2000] if isinstance(i, dict) and i.get("id")}
        HostDockerUsage.objects.update_or_create(host=task.host, defaults={
            "read_at": _now(), "images_size": n("images_size"), "images_reclaimable": n("images_reclaimable"),
            "containers_size": n("containers_size"), "volumes_size": n("volumes_size"),
            "volumes_reclaimable": n("volumes_reclaimable"), "build_cache_size": n("build_cache_size"),
            "build_cache_reclaimable": n("build_cache_reclaimable"),
            "volume_sizes": volume_sizes, "image_containers": image_containers,
        })
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Docker usage ingest failed for task %s", task.id)


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


__all__ = ["_clean_hunt_match", "_clean_step_results", "_ingest_hunt_matches", "_maybe_apply_playbook_completion_tag", "_maybe_apply_tags", "_maybe_capture_inventory_column", "_maybe_ingest_docker_usage", "_maybe_ingest_firewall_rules", "_maybe_ingest_trivy_report", "_maybe_request_nessus_scan", "_maybe_run_alert_actions", "_named_tags", "_tag_steps"]
