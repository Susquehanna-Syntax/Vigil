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
        if tasks:
            logger.debug("Monitor mode — ignoring %d task(s)", len(tasks))
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

        # TTL check
        ttl = task.get("ttl_seconds", 300)
        created_str = task.get("created_at")
        if created_str:
            try:
                created = datetime.fromisoformat(created_str)
                if datetime.now(timezone.utc) > created.replace(tzinfo=timezone.utc) + timedelta(seconds=ttl):
                    logger.warning("Task %s has expired (TTL %ds) — rejecting", task_id, ttl)
                    _report_rejected(config, task, f"Task expired (TTL {ttl}s)")
                    nonce_store.record(nonce)
                    continue
            except (ValueError, TypeError):
                pass  # If no created_at, skip TTL check (server may not send it)

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

    Each step is validated against the agent's local allowlist individually.
    If a step is disallowed or fails, execution stops (fail-fast) and we
    report the aggregated result back to the server.
    """
    # Translate the server's spec format into what the runtime expects:
    # server sends {steps: [{action, params, id}, ...]}
    # runtime expects {steps: [{action, params, name}, ...]}
    raw_steps = params.get("steps", [])
    def _build_step(s: dict, i: int) -> dict:
        d: dict = {
            "name": s.get("id", s.get("name", f"step{i+1}")),
            "action": s.get("action", s.get("type", "")),
            "params": s.get("params", {}),
        }
        if s.get("success_criteria"):
            d["success_criteria"] = s["success_criteria"]
        return d

    runtime_payload = {
        "steps": [_build_step(s, i) for i, s in enumerate(raw_steps)],
        "variables": params.get("variables", {}),
    }

    runtime = TaskRuntime(runtime_payload, config)
    results = runtime.run()

    # Build per-step output for the server
    step_outputs = []
    any_error = False
    for r in results:
        status = "OK" if r.state == "ok" else "ERROR"
        line = f"[{status}] {r.name}: {r.output or r.error or r.state}"
        step_outputs.append(line)
        if r.state == "error":
            any_error = True

    output = "\n".join(step_outputs)

    if any_error:
        logger.warning("Script task %s failed at step %r", task_id, results[-1].name)
        _report_failed(config, task, output)
    else:
        logger.info("Script task %s completed (%d steps)", task_id, len(results))
        _report_completed(config, task, output)


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
    consecutive_failures = 0
    inventory_refresh_after = 0.0  # monotonic deadline; 0 → refresh now
    last_inventory: dict | None = None
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
