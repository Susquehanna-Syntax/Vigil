"""apps.tasks.views: shared helpers (sequence), moved unchanged."""
from ._common import *  # noqa: F401,F403
from ._ingest import *  # noqa: F401,F403


def _merged_outputs(task: Task) -> dict:
    """Every output the task's steps produced, in order — a later step wins
    on a name clash. relevant: probes are evidence, not outputs."""
    merged: dict = {}
    for step in (task.result_data or {}).get("steps") or []:
        if isinstance(step, dict) and not str(step.get("id", "")).startswith("relevant-"):
            result = step.get("result")
            if isinstance(result, dict):
                merged.update(result)
    return merged


def _resolve_goto(goto: Task, tasks: list) -> None:
    """Resolve a go to the release walk reached (QA-36); it never reaches an agent.

    A target later in the chain that has not run yet: everything blocked in
    between is skipped (branches converge). Otherwise the target ran earlier:
    the chain from its newest run through this go to is cloned right after the
    go to (a retry loop), at most ``max`` times per host run; after that the
    go to does nothing and the host goes on.
    """
    target = goto.params.get("goto")
    gid = goto.params.get("goto_id")
    limit = int(goto.params.get("max") or 3)
    stamp = now()
    later = [t for t in tasks if t.step_order > goto.step_order]
    dest = next((t for t in later
                 if t.step_ref == target and t.state == Task.State.BLOCKED), None)
    if dest is not None:
        for t in later:
            if t.step_order >= dest.step_order:
                break
            if t.state == Task.State.BLOCKED:
                t.state = Task.State.SKIPPED
                t.completed_at = stamp
                t.result_output = f"[SKIPPED] jumped over by go to {target}"
                t.save(update_fields=["state", "completed_at", "result_output"])
        goto.result_output = f"[GOTO] jumped ahead to {target}"
    else:
        start = next((t for t in reversed(tasks)
                      if t.step_order < goto.step_order and t.step_ref == target), None)
        jumps = Task.objects.filter(
            run=goto.run, host=goto.host, action="_goto", params__goto_id=gid,
            result_output__startswith="[GOTO] jumped back").count()
        if start is None:
            goto.result_output = f"[GOTO] {target} not found — going on"
        elif jumps >= limit:
            goto.result_output = f"[GOTO] went back to {target} {limit} times already — going on"
        else:
            templates = [t for t in tasks if start.step_order <= t.step_order <= goto.step_order]
            Task.objects.filter(
                run=goto.run, host=goto.host, step_order__gt=goto.step_order,
            ).update(step_order=F("step_order") + len(templates))
            for j, t in enumerate(templates):
                expires_at = None
                if t.expires_at and t.created_at:
                    expires_at = stamp + (t.expires_at - t.created_at)
                Task.objects.create(
                    host=t.host, run=t.run, requested_by=t.requested_by,
                    step_label=t.step_label, action=t.action, params=t.params,
                    risk_level=t.risk_level, ttl_seconds=t.ttl_seconds,
                    schedule=t.schedule, max_retries=t.max_retries,
                    retry_delay_seconds=t.retry_delay_seconds,
                    on_failure=t.on_failure, on_not_applicable=t.on_not_applicable,
                    step_ref=t.step_ref, branch=t.branch, guard=t.guard,
                    state=Task.State.BLOCKED, step_order=goto.step_order + 1 + j,
                    expires_at=expires_at, nonce=secrets.token_hex(32),
                )
            goto.result_output = f"[GOTO] jumped back to {target} ({jumps + 1}/{limit})"
    goto.state = Task.State.COMPLETED
    goto.completed_at = stamp
    goto.save(update_fields=["state", "completed_at", "result_output"])


def _release_next(run: TaskRun, host) -> None:
    """Release the host's next runnable task in *run* (M6 08b).

    Walks the host's blocked tasks in order. A task with a playbook-branch
    guard is judged against the finished steps before it: false → it is
    skipped ("branch … not taken") and the walk goes on; true, or no guard →
    it becomes pending and the walk stops. A guard that cannot be evaluated
    fails that task, and the rest of the chain follows the failure path.
    """
    from ..expression import evaluate

    tasks = list(Task.objects.filter(run=run, host=host).order_by("step_order"))
    context = {
        t.step_ref: {"status": _BRANCH_STATUS[t.state], "result": _merged_outputs(t)}
        for t in tasks if t.step_ref and t.state in _BRANCH_STATUS
    }
    for task in tasks:
        if task.state != Task.State.BLOCKED:
            continue
        if task.guard:
            try:
                runnable = bool(evaluate(task.guard, {"steps": context}))
            except Exception as exc:  # noqa: BLE001 — recorded on the task
                task.state = Task.State.FAILED
                task.completed_at = now()
                task.result_output = f"[ERROR] branch condition could not be evaluated: {exc}"
                task.save(update_fields=["state", "completed_at", "result_output"])
                _advance_run_sequence(task)
                return
            if not runnable:
                task.state = Task.State.SKIPPED
                task.completed_at = now()
                task.result_output = f"[SKIPPED] branch {task.branch} not taken"
                task.save(update_fields=["state", "completed_at", "result_output"])
                if task.step_ref:
                    context[task.step_ref] = {"status": "skipped", "result": {}}
                continue
        if task.action == "_goto":
            _resolve_goto(task, tasks)
            _release_next(run, host)
            return
        task.state = Task.State.PENDING
        task.save(update_fields=["state"])
        return


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
    if finished_task.state == Task.State.NOT_APPLICABLE:
        if finished_task.on_not_applicable == "skip":
            # This playbook step said "not applicable, go on to the next":
            # release the next runnable step exactly like COMPLETED.
            _release_next(run, finished_task.host)
        else:
            # The task did not apply to this host (or the step chose
            # "stop"): its remaining steps never run (unlike a failure,
            # which retries), and none of it is counted as a retry.
            sibling_qs.filter(state=Task.State.BLOCKED).update(
                state=Task.State.NOT_APPLICABLE,
                completed_at=now(),
            )
    elif finished_task.state in (Task.State.COMPLETED, Task.State.SKIPPED):
        _release_next(run, finished_task.host)
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

        if finished_task.on_failure == "continue":
            # The playbook handles this failure: go on, and let a later
            # branch (steps.<id>.status == "failed") decide what runs.
            _release_next(run, finished_task.host)
            _finalize_run_if_done(run)
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

    from ..summary import run_outcome

    run.state = run_outcome(Task.objects.filter(run=run).values_list("state", "on_failure"))
    run.finished_at = now()
    run.save(update_fields=["state", "finished_at"])


__all__ = ["_advance_run_sequence", "_finalize_run_if_done", "_merged_outputs", "_release_next", "_resolve_goto"]
