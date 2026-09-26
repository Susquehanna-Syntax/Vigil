"""Vigil agent entry point.

Usage:
    python -m vigil_agent                          # default config search
    python -m vigil_agent -c /etc/vigil/agent.yml  # explicit config path
"""

import argparse
import json
import logging
import os
import signal
import sys
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


def _relevant_holds(node, counts: dict[str, bool]) -> bool:
    """Fold a normalised ``relevant:`` tree against probe outcomes.

    ``all`` = every item holds, ``any`` = at least one holds, ``not`` = none
    holds; a probe item holds when its hunt's count was non-zero.
    """
    if "probe" in node:
        return counts.get(node["probe"]["id"], False)
    op = node["op"]
    if op == "all":
        return all(_relevant_holds(item, counts) for item in node["items"])
    if op == "any":
        return any(_relevant_holds(item, counts) for item in node["items"])
    return not any(_relevant_holds(item, counts) for item in node["items"])


def _probe_id_num(probe_id: str) -> int:
    try:
        return int(probe_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _evaluate_relevant(tree: dict, config) -> tuple[bool, list[dict], list[str]]:
    """Run every probe of a ``relevant:`` tree once and fold the decision.

    Returns ``(applies, probe_steps, probe_lines)`` where ``probe_steps`` is
    the per-probe evidence (the same shape a hunt step reports) and
    ``probe_lines`` is the output text. A probe that raises is reported
    inline as a task failure (``[ERROR] relevant-<n> (<type>): … — task not
    run``) and the evaluation stops.
    """
    probes = []
    seen = set()

    def collect(node) -> None:
        for item in node["items"]:
            if "probe" in item:
                probe = item["probe"]
                if probe["id"] not in seen:
                    seen.add(probe["id"])
                    probes.append(probe)
            else:
                collect(item)

    collect(tree)
    probes.sort(key=lambda p: _probe_id_num(p["id"]))

    counts: dict[str, bool] = {}
    steps: list[dict] = []
    lines: list[str] = []
    for probe in probes:
        try:
            # Looked up at call time, as TaskRuntime does for steps, so one
            # executor (and one allowlist check) serves probes and steps.
            from .executor import execute_action as _execute
            out = _execute(probe["type"], probe["params"], config)
        except Exception as exc:
            line = f"[ERROR] {probe['id']} ({probe['type']}): {exc} — task not run"
            raise RelevantProbeError("\n".join(lines + [line]), steps) from exc
        count = (out.data or {}).get("count")
        counts[probe["id"]] = isinstance(count, int) and count > 0
        step = {"id": probe["id"], "status": "ok", "result": dict(out.data or {})}
        # The probe runs as the hunt it is, so its evidence carries the same
        # hunt block a hunt step reports.
        hunt = _hunt_block(probe["type"], str(out))
        if hunt is not None:
            step["hunt"] = hunt
        steps.append(step)
        lines.append(
            f"[PROBE] {probe['id']} ({probe['type']}): "
            f"{count if isinstance(count, int) else '?'} match(es)"
        )

    return _relevant_holds(tree, counts), steps, lines


class RelevantProbeError(Exception):
    """A ``relevant:`` probe failed; ``str(exc)`` is the report line and
    ``steps`` the evidence of the probes that ran before it."""

    def __init__(self, line: str, steps: list[dict]):
        super().__init__(line)
        self.steps = steps


def _hunt_payload(step_result) -> dict | None:
    """The ``hunt`` block to attach to a hunt step's report, or None.

    A hunt step's output is the JSON text run_hunt returns; if it parses and
    carries a ``matches`` list, the matches plus the hunt's own bookkeeping
    travel with the report so the server can store them.
    """
    return _hunt_block(step_result.action, step_result.output)


def _hunt_block(action: str | None, output) -> dict | None:
    """``_hunt_payload`` for a bare (action, output) pair — used for probes."""
    if not (action or "").startswith("hunt_"):
        return None
    try:
        body = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
        return None
    return {
        "matches": body["matches"],
        "truncated": body.get("truncated", False),
        "duration": body.get("duration"),
        "timed_out": body.get("timed_out", False),
    }


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

    # Evaluate the relevant: tree before any step runs: every probe is the
    # hunt it is (allowlist and hunt limits apply), the tree decides, and a
    # non-matching host reports not_applicable with the probes' evidence.
    # Probe evidence is prepended to the steps/output of whatever report the
    # task ends with.
    probe_steps: list[dict] = []
    probe_lines: list[str] = []
    relevant = params.get("relevant")
    if isinstance(relevant, dict):
        try:
            applies, probe_steps, probe_lines = _evaluate_relevant(relevant, config)
        except RelevantProbeError as exc:
            logger.warning("Script task %s: %s", task_id, exc)
            _report_failed(config, task, str(exc), steps=exc.steps)
            return
        if not applies:
            output = "\n".join(
                probe_lines + ["[NOT APPLICABLE] relevant: did not match"]
            )
            logger.info("Script task %s not applicable", task_id)
            _report_not_applicable(config, task, output, steps=probe_steps)
            return

    # Build the evaluation context once per task. agent.* comes from the
    # platform; inputs.* from the resolved step inputs the server already
    # substituted server-side, but we also pass anything in
    # params.variables for back-compat.
    context = _build_when_context(config, params)

    # Parse-check every step's when: before anything runs. The actual
    # evaluation happens in TaskRuntime._execute_steps (per step, with the
    # earlier steps' results in context), but a predicate that cannot even
    # be parsed — version drift between server and agent, the server having
    # validated syntax at save time — fails the WHOLE task before any step
    # runs: executing half a script under drift is worse than executing none.
    for i, s in enumerate(raw_steps):
        when_expr = (s.get("when") or "").strip()
        if when_expr:
            try:
                from .expression import parse as _parse_when
                _parse_when(when_expr)
            except Exception as exc:
                name = s.get("id", s.get("name", f"step{i+1}"))
                msg = (
                    f"[ERROR] {name}: when {when_expr!r} could not be "
                    f"evaluated ({exc}) — task aborted before execution"
                )
                logger.warning("Script task %s: %s", task_id, msg)
                _report_failed(config, task, msg)
                return

    runnable_steps = [
        {
            "name": s.get("id", s.get("name", f"step{i+1}")),
            "action": s.get("action", s.get("type", "")),
            "params": s.get("params", {}),
            **({"success_criteria": s["success_criteria"]} if s.get("success_criteria") else {}),
            **({"when": (s.get("when") or "").strip()} if (s.get("when") or "").strip() else {}),
        }
        for i, s in enumerate(raw_steps)
    ]

    runtime_payload = {
        "steps": runnable_steps,
        "variables": params.get("variables", {}),
        "when_context": context,
    }
    runtime = TaskRuntime(runtime_payload, config)
    results = runtime.run()

    steps = list(probe_steps)
    for r in results:
        step = {"id": r.name, "status": r.state, "result": dict(r.data)}
        hunt_payload = _hunt_payload(r)
        if hunt_payload is not None:
            step["hunt"] = hunt_payload
        steps.append(step)

    step_outputs = list(probe_lines)
    any_error = False
    any_ran = False
    for r in results:
        if r.state == "skipped":
            step_outputs.append(f"[SKIPPED] {r.name}: {r.output}")
            continue
        any_ran = True
        status = "OK" if r.state == "ok" else "ERROR"
        step_outputs.append(f"[{status}] {r.name}: {r.output or r.error or r.state}")
        if r.state == "error":
            any_error = True

    output = "\n".join(step_outputs)

    if any_error:
        logger.warning("Script task %s failed", task_id)
        _report_failed(config, task, output, steps=steps)
    elif not any_ran:
        # Every step's when: predicate was false — the task ran
        # successfully in the sense that nothing went wrong; nothing
        # was applicable.
        logger.info("Script task %s skipped — no step matched when: predicates", task_id)
        _report_skipped(config, task, output, steps=steps)
    else:
        logger.info("Script task %s completed (%d step(s) ran)", task_id, len(results))
        _report_completed(config, task, output, steps=steps)


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


def _report_completed(config, task: dict, output: str, steps=None) -> None:
    try:
        client.report_result(config, task["id"], "completed", output, steps=steps)
    except Exception:
        logger.exception("Failed to report task %s result", task.get("id"))


def _report_rejected(config, task: dict, reason: str) -> None:
    try:
        client.report_result(config, task["id"], "rejected", reason)
    except Exception:
        logger.exception("Failed to report task %s rejection", task.get("id"))


def _report_failed(config, task: dict, error: str, steps=None) -> None:
    try:
        client.report_result(config, task["id"], "failed", error, steps=steps)
    except Exception:
        logger.exception("Failed to report task %s failure", task.get("id"))


def _report_not_applicable(config, task: dict, output: str, steps=None) -> None:
    try:
        client.report_result(config, task["id"], "not_applicable", output, steps=steps)
    except Exception:
        logger.exception("Failed to report task %s as not applicable", task.get("id"))


def _report_skipped(config, task: dict, output: str, steps=None) -> None:
    try:
        client.report_result(config, task["id"], "skipped", output, steps=steps)
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

    # `allow-script` is a local admin command, not a task action — it must
    # never reach the executor. Dispatch before the agent parser so its flags
    # (which include its own -c) are handled by it.
    if len(sys.argv) > 1 and sys.argv[1] == "allow-script":
        from .allowscript import main as _allowscript_main

        sys.exit(_allowscript_main(sys.argv[2:], _cli_config_path))

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
