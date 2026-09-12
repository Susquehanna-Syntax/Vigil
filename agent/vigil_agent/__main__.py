"""Vigil agent entry point.

Usage:
    python -m vigil_agent                          # default config search
    python -m vigil_agent -c /etc/vigil/agent.yml  # explicit config path
"""

import argparse
import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import client, collector, verify, windows_update
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


def request_shutdown() -> None:
    """Ask the check-in loop to stop after its current cycle.

    The Windows service host has no signal to send — SvcStop calls this, which
    is the same path SIGTERM takes on Linux.
    """
    global _shutdown
    logger.info("Shutdown requested, finishing current cycle")
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
            # the local allowlist each time.  Same safety net as the
            # single-action path below — an unexpected exception must
            # report `failed` rather than leave the task DISPATCHED on
            # the server forever.
            try:
                _execute_script_task(task_id, params, config, task)
            except Exception as exc:
                logger.exception("Script task %s crashed", task_id)
                _report_failed(config, task, f"Agent error: {exc}")
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
    # TaskRuntime at all. An expression that cannot be evaluated at all
    # (version drift between server and agent — the server validated
    # syntax at save time) fails the WHOLE task before any step runs:
    # executing half a script under drift is worse than executing none.
    plan: list[tuple[int, dict, bool, str]] = []
    for i, s in enumerate(raw_steps):
        when_expr = (s.get("when") or "").strip()
        skip = False
        skip_reason = ""
        if when_expr:
            try:
                from .expression import evaluate as _eval_when
                if not _eval_when(when_expr, context):
                    skip = True
                    skip_reason = when_expr
            except Exception as exc:
                name = s.get("id", s.get("name", f"step{i+1}"))
                msg = (
                    f"[ERROR] {name}: when {when_expr!r} could not be "
                    f"evaluated ({exc}) — task aborted before execution"
                )
                logger.warning("Script task %s: %s", task_id, msg)
                _report_failed(config, task, msg)
                return
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


def _warn_on_privilege_mismatch(config) -> None:
    """Say so when the mode needs root and this process does not have it.

    The installer runs a monitor-mode agent as the unprivileged 'vigil-agent'
    user, because reading /proc needs nothing more. If someone later edits
    agent.yml to managed or full_control, the service unit still says
    User=vigil-agent — and without this the only symptom is every task failing
    with a permission error, one at a time, for as long as it takes someone to
    connect the two facts.

    This used to advise removing the User= line, which is not sufficient and
    was found not to be by running it: the monitor-mode unit also carries
    ProtectSystem=strict, and under that /boot is read-only *even for root* —

        # systemd-run --property=ProtectSystem=strict \
            /bin/sh -c 'id -u; mkdir -p /boot/vigil-probe-test'
        0
        mkdir: Read-only file system

    so a reprovision stage still fails with "[Errno 30] Read-only file
    system: '/boot/vigil-reprovision'". Re-running the installer regenerates
    the unit from the mode in agent.yml and is the only complete fix.

    A warning, not a refusal: an agent that stops monitoring because it cannot
    execute is worse than one that monitors and says it cannot execute.
    """
    if config.mode == "monitor":
        return
    try:
        if os.geteuid() == 0:
            return
    except AttributeError:      # Windows has no geteuid
        return

    logger.warning(
        "Mode is %r but this agent is not running as root (uid=%d). Task "
        "execution needs root — systemctl, package installs and firewall "
        "changes will all fail. Either set mode back to 'monitor', or "
        "re-run the installer so the unit is regenerated for this mode: "
        "curl -fsSL <server>/agent/install.sh | sudo bash. Editing the unit "
        "by hand is not enough — dropping 'User=' still leaves "
        "ProtectSystem=strict, which makes /boot and /etc read-only even for "
        "root, so reprovision and package installs keep failing.",
        config.mode, os.geteuid())


#: Set by main() so run_agent() can be called with no arguments from the
#: Windows service host, which does not get the command line.
_cli_config_path: Path | None = None


def main() -> None:
    global _cli_config_path

    parser = argparse.ArgumentParser(description="Vigil monitoring agent")
    parser.add_argument("-c", "--config", type=Path, help="Path to agent.yml")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--service",
        action="store_true",
        help="Run as a Windows service (used by install.ps1; not for interactive use)",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Write logs here instead of stdout. Implied by --service, which has no console.",
    )
    args = parser.parse_args()

    _cli_config_path = args.config

    if args.service and os.name != "nt":
        # Refuse before touching the filesystem. The ProgramData fallback below
        # is a Windows path; on Linux it is just a filename containing a
        # backslash, and mkdir(parents=True) duly created a directory called
        # "C:\ProgramData" in the working directory. Found exactly that way.
        parser.error("--service is only supported on Windows")

    log_file = args.log_file
    if args.service and log_file is None:
        # A service has no stdout. Without this every log line goes nowhere and
        # a misbehaving agent is undiagnosable.
        log_file = Path(os.environ["ProgramData"]) / "Vigil" / "agent.log"
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, KeyError):
            log_file = None

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="[%(asctime)s] %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        **({"filename": str(log_file)} if log_file else {}),
    )

    if args.service:
        from .winservice import run_service

        raise SystemExit(run_service())

    run_agent()


def run_agent() -> None:
    """The check-in loop. Called directly by main(), or on a worker thread by
    the Windows service host."""
    config = load_config(_cli_config_path)
    _warn_on_privilege_mismatch(config)
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

    # Only the main thread may install signal handlers. Under the Windows
    # service host this runs on a worker thread, where signal.signal() raises
    # ValueError — which would kill the loop before its first check-in.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    verify_key = verify.get_pinned_key(config.data_dir)

    # ── Main checkin loop ────────────────────────────────────────────────
    # Hardware inventory shifts at human timescales — refresh once per hour
    # rather than on every checkin to avoid wasting cycles on dmidecode.
    # Docker image update checks hit the Docker Hub registry, which rate-
    # limits anonymous clients. Even with HEAD requests (which don't count
    # against the pull budget) there's no reason to re-check often — a
    # published image moving is a once-in-days event. The interval comes
    # from `docker_check_interval` in agent.yml (default 6 hours, floor 5
    # minutes); a docker-mutating task or a check_docker_updates task sets
    # the collector's recheck flag, which forces a refresh on the next pass
    # so alerts fire/resolve promptly after remediation. Results are shipped
    # once per refresh — re-sending them on every checkin would only insert
    # duplicate points with stale timestamps the alert engine ignores.
    consecutive_failures = 0
    inventory_refresh_after = 0.0  # monotonic deadline; 0 → refresh now
    docker_refresh_after = 0.0    # monotonic deadline; 0 → check now
    last_inventory: dict | None = None
    _cached_docker_metrics: list[dict] = []
    docker_payload_pending = False  # fresh results awaiting a successful checkin
    while not _shutdown:
        try:
            metrics = collector.collect_all(config)
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
            if collector.consume_docker_recheck():
                docker_refresh_after = 0.0
            if time.monotonic() >= docker_refresh_after:
                try:
                    _cached_docker_metrics = collector.collect_docker_updates()
                    docker_payload_pending = True
                except Exception:
                    logger.exception("Docker update check failed")
                docker_refresh_after = time.monotonic() + config.docker_check_interval
            if docker_payload_pending:
                metrics.extend(_cached_docker_metrics)
            # Container snapshot is local-only (Docker socket) and cheap enough
            # to refresh every checkin so the monitor shows live stats. None
            # when Docker is unavailable — the server then keeps the last set.
            try:
                docker_containers = collector.collect_docker_containers()
            except Exception:
                logger.exception("Docker container collection failed")
                docker_containers = None
            response = client.checkin(
                config, metrics, inventory=inventory_payload,
                docker_containers=docker_containers,
                reboot_required=collector.reboot_required(),
                windows_updates=windows_update.summary(),
            )
            consecutive_failures = 0
            docker_payload_pending = False

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
