"""Vigil agent entry point.

Usage:
    python -m vigil_agent                          # default config search
    python -m vigil_agent -c /etc/vigil/agent.yml  # explicit config path
"""

import argparse
import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import client, collector, verify
from .config import load_config
from .executor import execute_action
from .nonce_store import NonceStore
from .runtime import TaskRuntime
from .verify import KeyMismatchError

logger = logging.getLogger("vigil")

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    logger.info("Received signal %s, shutting down gracefully", signum)
    _shutdown = True


def _process_tasks(tasks: list[dict], config, nonce_store: NonceStore, verify_key) -> None:
    """Process tasks received from the server."""
    if config.mode == "monitor":
        # Reject explicitly rather than silently dropping — otherwise tasks sit
        # in DISPATCHED forever on the server and the operator has no idea why.
        for task in tasks:
            logger.info(
                "Monitor mode — rejecting task %s (%s)",
                task.get("id"), task.get("action"),
            )
            _report_rejected(
                config, task,
                "Agent is in monitor mode — task execution disabled in agent config",
            )
        return

    if verify_key is None:
        if tasks:
            logger.warning(
                "No pinned public key — cannot verify task signatures. "
                "Rejecting %d task(s).",
                len(tasks),
            )
            for task in tasks:
                _report_rejected(config, task, "No public key available for signature verification")
        return

    for task in tasks:
        task_id = task.get("id", "unknown")
        action = task.get("action", "")
        nonce = task.get("nonce", "")

        # Replay protection
        if nonce_store.seen(nonce):
            logger.warning("Task %s has replayed nonce — rejecting", task_id)
            _report_rejected(config, task, "Replayed nonce")
            continue

        # TTL check — bounded against when the SERVER dispatched the task,
        # not when it was originally created. A task can sit in PENDING for
        # hours (waiting on a schedule.window, retry delay, or offline host);
        # the TTL only makes sense once the signed payload is on the wire.
        # Falls back to ``created_at`` for compatibility with older servers.
        ttl = task.get("ttl_seconds", 300)
        ref_str = task.get("dispatched_at") or task.get("created_at")
        if ref_str:
            try:
                ref = datetime.fromisoformat(ref_str)
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > ref + timedelta(seconds=ttl):
                    logger.warning("Task %s has expired (TTL %ds) — rejecting", task_id, ttl)
                    _report_rejected(config, task, f"Task expired (TTL {ttl}s)")
                    nonce_store.record(nonce)
                    continue
            except (ValueError, TypeError):
                pass  # malformed timestamp — skip the gate, signature still gates execution

        # Signature verification
        if not verify.verify_task_signature(task, verify_key):
            logger.warning("Task %s failed signature verification — rejecting", task_id)
            _report_rejected(config, task, "Invalid signature")
            nonce_store.record(nonce)
            continue

        # Execution — the agent validates each action against its own local
        # config.  The server sends the script; we decide what's allowed.
        nonce_store.record(nonce)
        params = task.get("params", {})

        if isinstance(params.get("steps"), list):
            # ── Multi-step script ───────────────────────────────────────
            # The full script was sent as a single signed payload.  The
            # TaskRuntime calls execute_action() per step, which checks
            # the local allowlist each time.
            _execute_script_task(task_id, params, config, task)
        else:
            # ── Single-action task (legacy / quick-action) ──────────────
            try:
                output = execute_action(action, params, config)
                logger.info("Task %s (%s) completed", task_id, action)
                _report_completed(config, task, output)
            except ValueError as exc:
                logger.warning("Task %s (%s) rejected: %s", task_id, action, exc)
                _report_rejected(config, task, str(exc))
            except Exception as exc:
                logger.error("Task %s (%s) failed: %s", task_id, action, exc)
                _report_failed(config, task, str(exc))


def _execute_script_task(task_id: str, params: dict, config, task: dict) -> None:
    """Run a multi-step script through the TaskRuntime.

    Each step is validated against the agent's local allowlist
    individually. Per-step ``when:`` expressions are evaluated *here*
    before the runtime sees them — steps that evaluate false are
    skipped, recorded in the output, and don't block subsequent steps.
    If a non-skipped step fails, execution stops (fail-fast) and we
    report the aggregated result back to the server.
    """
    raw_steps = params.get("steps", [])

    # Build the evaluation context once per task. agent.* comes from the
    # platform; inputs.* from the resolved step inputs the server already
    # substituted server-side, but we also pass anything in
    # params.variables for back-compat.
    context = _build_when_context(config, params)

    # Pre-evaluate every step's when:. Skipped steps don't reach
    # TaskRuntime at all.
    plan: list[tuple[int, dict, bool, str]] = []
    for i, s in enumerate(raw_steps):
        when_expr = (s.get("when") or "").strip()
        skip = False
        skip_reason = ""
        if when_expr:
            try:
                from .expression import evaluate as _eval_when, ExprError
                if not _eval_when(when_expr, context):
                    skip = True
                    skip_reason = when_expr
            except ExprError as exc:
                # Treat unparseable when: as fail-loud — the server
                # already validated syntax, so reaching here means a
                # version drift between server + agent. Better to
                # surface than silently run something untested.
                logger.warning("when: evaluation failed: %s", exc)
                skip = True
                skip_reason = f"when expr error: {exc}"
        plan.append((i, s, skip, skip_reason))

    runnable_steps = [
        {
            "name": s.get("id", s.get("name", f"step{i+1}")),
            "action": s.get("action", s.get("type", "")),
            "params": s.get("params", {}),
            **({"success_criteria": s["success_criteria"]} if s.get("success_criteria") else {}),
        }
        for i, s, skip, _ in plan if not skip
    ]

    if runnable_steps:
        runtime_payload = {
            "steps": runnable_steps,
            "variables": params.get("variables", {}),
        }
        runtime = TaskRuntime(runtime_payload, config)
        results = runtime.run()
    else:
        results = []  # everything got skipped

    # Walk the original plan and stitch results back in for step_outputs.
    results_by_name = {r.name: r for r in results}
    step_outputs = []
    any_error = False
    any_ran = False
    for i, s, skip, reason in plan:
        name = s.get("id", s.get("name", f"step{i+1}"))
        if skip:
            step_outputs.append(f"[SKIPPED] {name}: when {reason!r} evaluated false")
            continue
        any_ran = True
        r = results_by_name.get(name)
        if r is None:
            step_outputs.append(f"[ERROR] {name}: runtime returned no result")
            any_error = True
            continue
        status = "OK" if r.state == "ok" else "ERROR"
        step_outputs.append(f"[{status}] {r.name}: {r.output or r.error or r.state}")
        if r.state == "error":
            any_error = True

    output = "\n".join(step_outputs)

    if any_error:
        logger.warning("Script task %s failed", task_id)
        _report_failed(config, task, output)
    elif not any_ran:
        # Every step's when: predicate was false — the task ran
        # successfully in the sense that nothing went wrong; nothing
        # was applicable.
        logger.info("Script task %s skipped — no step matched when: predicates", task_id)
        _report_skipped(config, task, output)
    else:
        logger.info("Script task %s completed (%d step(s) ran)", task_id, len(results))
        _report_completed(config, task, output)


def _build_when_context(config, params: dict) -> dict:
    """Build the ``{agent, inputs, host}`` dict used by when: predicates.

    Pulled fresh per task so changes in platform state (e.g. a package
    manager installed mid-life) are picked up by the next deploy. The
    cost is one ``pkg_manager.detect()`` call per task, which is cheap
    (it's just ``which apt`` / ``which dnf`` etc.).
    """
    import platform as _plat
    try:
        from .pkg_manager import detect as _detect_pkg
        _pm = _detect_pkg()
        pkg = _pm.name if _pm else ""
    except Exception:
        pkg = ""

    machine = (_plat.machine() or "").lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine.startswith("arm"):
        arch = "arm"
    else:
        arch = machine

    sysname = (_plat.system() or "").lower()
    os_name = (
        "linux" if sysname == "linux"
        else "darwin" if sysname == "darwin"
        else "windows" if sysname == "windows"
        else sysname
    )

    return {
        "agent": {
            "os": os_name,
            "arch": arch,
            "pkg_manager": pkg,
            "hostname": _plat.node(),
        },
        "inputs": (params.get("variables") or {}),
        "host": {},   # reserved for future server-pushed context
    }


def _report_completed(config, task: dict, output: str) -> None:
    try:
        client.report_result(config, task["id"], "completed", output)
    except Exception:
        logger.exception("Failed to report task %s result", task.get("id"))


def _report_rejected(config, task: dict, reason: str) -> None:
    try:
        client.report_result(config, task["id"], "rejected", reason)
    except Exception:
        logger.exception("Failed to report task %s rejection", task.get("id"))


def _report_failed(config, task: dict, error: str) -> None:
    try:
        client.report_result(config, task["id"], "failed", error)
    except Exception:
        logger.exception("Failed to report task %s failure", task.get("id"))


def _report_skipped(config, task: dict, output: str) -> None:
    try:
        client.report_result(config, task["id"], "skipped", output)
    except Exception:
        logger.exception("Failed to report task %s skip", task.get("id"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Vigil monitoring agent")
    parser.add_argument("-c", "--config", type=Path, help="Path to agent.yml")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="[%(asctime)s] %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)
    logger.info(
        "Vigil agent starting — server=%s mode=%s interval=%ds",
        config.server_url,
        config.mode,
        config.checkin_interval,
    )

    # Ensure data directory exists
    config.data_dir.mkdir(parents=True, exist_ok=True)

    nonce_store = NonceStore(config.data_dir)

    # Register with the server
    try:
        client.register(config)
    except Exception:
        logger.exception("Registration failed — will retry on first checkin")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    verify_key = verify.get_pinned_key(config.data_dir)

    # ── Main checkin loop ────────────────────────────────────────────────
    # Hardware inventory shifts at human timescales — refresh once per hour
    # rather than on every checkin to avoid wasting cycles on dmidecode.
    # Docker image update checks are refreshed every 5 minutes; the cached
    # results are re-included in every checkin so the server alert engine
    # always has recent data without hammering the Docker Hub registry.
    _DOCKER_CHECK_INTERVAL = 300  # 5 minutes
    consecutive_failures = 0
    inventory_refresh_after = 0.0  # monotonic deadline; 0 → refresh now
    docker_refresh_after = 0.0    # monotonic deadline; 0 → check now
    last_inventory: dict | None = None
    _cached_docker_metrics: list[dict] = []
    while not _shutdown:
        try:
            metrics = collector.collect_all()
            inventory_payload = None
            if time.monotonic() >= inventory_refresh_after:
                try:
                    last_inventory = collector.collect_inventory()
                except Exception:
                    logger.exception("Inventory collection failed")
                    last_inventory = None
                # Always send on the first refresh; otherwise hourly.
                inventory_refresh_after = time.monotonic() + 3600
                inventory_payload = last_inventory
            if time.monotonic() >= docker_refresh_after:
                try:
                    _cached_docker_metrics = collector.collect_docker_updates()
                except Exception:
                    logger.exception("Docker update check failed")
                docker_refresh_after = time.monotonic() + _DOCKER_CHECK_INTERVAL
            metrics.extend(_cached_docker_metrics)
            response = client.checkin(config, metrics, inventory=inventory_payload)
            consecutive_failures = 0

            # Handle public key (TOFU pinning)
            pub_key_b64 = response.get("public_key")
            if pub_key_b64:
                try:
                    verify_key = verify.pin_public_key(config.data_dir, pub_key_b64)
                except KeyMismatchError:
                    logger.critical(
                        "SERVER PUBLIC KEY HAS CHANGED. This could indicate a compromised server. "
                        "All tasks will be rejected until the key pin is manually reset. "
                        "If this is a legitimate key rotation, delete %s/server_public_key.pin",
                        config.data_dir,
                    )
                    verify_key = None  # Reject all tasks from now on

            # Process tasks
            tasks = response.get("tasks", [])
            if tasks:
                _process_tasks(tasks, config, nonce_store, verify_key)

            status = response.get("status", "unknown")
            logger.debug("Checkin complete — status=%s tasks=%d", status, len(tasks))

        except Exception:
            consecutive_failures += 1
            # Back off on repeated failures, cap at 5 minutes
            backoff = min(consecutive_failures * config.checkin_interval, 300)
            logger.exception(
                "Checkin failed (attempt %d), next retry in %ds",
                consecutive_failures,
                backoff,
            )
            _sleep_interruptible(backoff)
            continue

        _sleep_interruptible(config.checkin_interval)

    logger.info("Agent shut down")


def _sleep_interruptible(seconds: float) -> None:
    """Sleep in small increments so signal handlers can interrupt promptly."""
    end = time.monotonic() + seconds
    while not _shutdown and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


if __name__ == "__main__":
    main()
