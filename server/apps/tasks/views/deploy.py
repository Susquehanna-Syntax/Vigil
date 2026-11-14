"""apps.tasks.views: deploy views (split from the old single module, unchanged)."""

from drf_spectacular.utils import extend_schema

from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403
from ._sequence import *  # noqa: F401,F403
from ._catalog import *  # noqa: F401,F403
from ..api_schema import (
    TasksDeployRequestSerializer,
    TasksDeployResponseSerializer,
    TasksErrorSerializer,
    TasksNotFoundSerializer,
)


@extend_schema(
    request=TasksDeployRequestSerializer,
    responses={
        201: TasksDeployResponseSerializer,
        400: TasksErrorSerializer,
        # The confirmation gate; run_denied's 403 for a host
        # the caller may not run tasks on.
        401: TasksErrorSerializer,
        403: TasksErrorSerializer,
        # run_denied's "not found" for an out-of-scope host,
        # and the definition or host lookups.
        404: TasksNotFoundSerializer,
    },
)
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
        if not isinstance(raw_tags, list) or not all(
            isinstance(t, str) for t in raw_tags
        ):
            return Response({"error": "tags must be a list of strings"}, status=400)
        wanted = {t.strip().lower() for t in raw_tags if t.strip()}
        if not wanted:
            return Response({"error": "tags is empty after normalization"}, status=400)
        from ..authz import runnable

        candidate_hosts = Host.objects.filter(status=Host.Status.ONLINE).exclude(
            mode=Host.Mode.MONITOR
        )
        # Only hosts this user may run tasks on: tag targeting must never
        # reach into a site the user cannot see (SEC-1).
        host_ids = [
            str(h.id)
            for h in runnable(request.user, candidate_hosts)
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

    raw_inputs = request.data.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        return Response({"error": "inputs must be an object"}, status=400)
    # The one way a definition becomes a signed task (apps/tasks/dispatch.py):
    # use: copied in, inputs resolved, inline playbooks expanded.
    from apps.playbooks.expansion import PlaybookExpandError, including_from
    from ..dispatch import resolve_task_spec, task_params

    # Includes may pull in a site's playbook only when every target is in that
    # site: its admins edit those steps (S1 04e).
    from django.core.exceptions import ValidationError as _BadId

    try:
        target_sites = {
            None if site.is_global else site.id
            for site in scoping.sites_for_hosts(host_ids).values()
        }
    except (_BadId, ValueError, TypeError):
        target_sites = set()  # the host checks below answer malformed ids
    try:
        with including_from(target_sites.pop() if len(target_sites) == 1 else None):
            spec = resolve_task_spec(definition, user=request.user, inputs=raw_inputs)
    except (SpecError, PlaybookExpandError) as exc:
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

    if not spec.get("actions"):
        return Response({"error": "definition has no actions"}, status=400)

    hosts = list(Host.objects.filter(id__in=host_ids))
    if len(hosts) != len(host_ids):
        return Response({"error": "One or more hosts not found"}, status=404)
    # A signed task runs as root on its host: the caller must be allowed to run
    # tasks on every target, in that target's site (SEC-1).
    from ..authz import run_denied

    denied = run_denied(request.user, hosts)
    if denied:
        return denied

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

    # Hosts whose agent would refuse an action in this task (its reported
    # allowlist lacks it) are left out and reported, unless the operator says
    # to send anyway — the agent stays the authority either way (M7).
    from ..refusals import refusals_for

    skipped = refusals_for(hosts, spec)
    if skipped and request.data.get("send_to_refusing") is not True:
        refusing = {row["host_id"] for row in skipped}
        hosts = [h for h in hosts if str(h.id) not in refusing]
        if not hosts:
            return Response(
                {
                    "error": "every selected host would refuse this task",
                    "skipped": skipped,
                },
                status=400,
            )
    elif skipped:
        skipped = []  # sent to them anyway: nothing was skipped

    # The full script is sent as a single signed task per host — the agent
    # validates each action against its own local allowlist, so a compromised
    # server cannot escalate beyond what each agent permits.
    try:
        params, risk, expires_at = task_params(spec)
    except (SpecError, PlaybookExpandError) as exc:
        return Response({"error": str(exc)}, status=400)
    steps_payload = params["steps"]

    schedule_snapshot = spec.get("schedule") or {}
    retry_cfg = (spec.get("on_failure") or {}).get("retry") or {}
    max_retries = int(retry_cfg.get("attempts", 0))
    retry_delay = int(retry_cfg.get("delay_seconds", 0))

    # The high-risk hold. CLAUDE.md's risk tiers say "high-risk (2FA + 60s
    # delay)" and apps/playbooks/models.py repeats it, but nothing ever set
    # not_before on a first dispatch — the only writer was the retry path — so
    # the pause an operator was told they had did not exist and a high-risk
    # task went to the agent at its very next check-in.
    #
    # The gate itself was already built and working: the check-in view holds
    # back any task whose not_before is in the future. Only the setting was
    # missing.
    hold_until = None
    if risk == "high":
        hold_until = now() + timedelta(seconds=HIGH_RISK_HOLD_SECONDS)

    # Hunts stop waiting for offline hosts after their stays_open (phase 06b);
    # non-hunt tasks never expire while pending.
    with transaction.atomic():
        run = TaskRun.objects.create(
            definition=definition,
            name_snapshot=definition.name,
            requested_by=request.user,
            host_count=len(hosts),
            step_count=len(steps_payload),
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
                params=params,
                risk_level=risk,
                state=Task.State.PENDING,
                not_before=hold_until,
                expires_at=expires_at,
                nonce=secrets.token_hex(32),
                schedule=schedule_snapshot,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay,
            )

    text_steps = _hunt_text_step_ids(steps_payload)
    if text_steps:
        _emit_hunt_text_requested(run, request.user, text_steps)

    return Response({**TaskRunSerializer(run).data, "skipped": skipped}, status=201)
