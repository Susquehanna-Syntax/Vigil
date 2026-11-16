"""System actions: script execution, user notification, reboot, run_command, hostname.

Moved out of executor.py. ``execute_script`` is the only action that receives
task inputs, mapped to ``VIGIL_INPUT_*`` environment variables so a value can
never become a second command on a command line; an inline body runs only when
its hash was approved on this host, and a named script only when nothing below
``scripts_dir`` is writable by someone other than root (SEC-2). ``reboot``
carries the user's deferral budget and the platform-specific ``shutdown``
argument shapes. Handlers go through ``executor._run`` — called as ``ex._run``
so a test that patches ``vigil_agent.executor._run`` still reaches them;
executor re-exports every name defined here.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from .. import executor as ex
from .. import scripthash
from ..config import AgentConfig
from ..deferral import RebootDeferral
from ..executor import _EXEC_TIMEOUT, _SAFE_HOSTNAME, _SAFE_SCRIPT_NAME, ActionOutput

logger = logging.getLogger("vigil.executor")


def _input_env(inputs: dict | None) -> dict[str, str]:
    """Map task inputs to ``VIGIL_INPUT_<NAME>`` environment variables.

    Inputs pasted into a command line would be code — ``nginx; rm -rf /``
    runs as a second command.  In an environment variable the same value is
    only data: ``printf '%s' "$VIGIL_INPUT_APP"`` prints it.
    """
    if not inputs:
        return {}
    env: dict[str, str] = {}
    for name, value in inputs.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
            continue
        if isinstance(value, str) and "\x00" in value:
            raise ValueError(f"input {name!r} contains a NUL byte")
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, float) and value.is_integer():
            value = str(int(value))
        else:
            value = str(value)
        env[f"VIGIL_INPUT_{str(name).upper()}"] = value
    return env


def _execute_inline_script(params: dict, config: AgentConfig, inputs: dict | None = None) -> str:
    """Run an inline ``script`` body with ``shell``.

    The body is arbitrary code from the server, so outside full_control it runs
    only if its exact hash is in ``allowed_script_hashes`` — approved on this
    host by its owner. Any edit changes the hash and needs a new approval.
    """
    if "script_name" in params:
        raise ValueError("give script_name or script, not both")

    shell = params.get("shell", "")
    if shell not in ("bash", "sh", "powershell", "pwsh"):
        raise ValueError(
            f"shell must be one of bash, sh, powershell, pwsh; got {shell!r}")

    body = params.get("script", "")
    if not isinstance(body, str) or not body:
        raise ValueError("script must be a non-empty string")
    if len(body) > 65536:
        raise ValueError("script body exceeds 65536 characters")

    digest = scripthash.script_hash(body)
    if config.mode != "full_control" and digest not in config.allowed_script_hashes:
        raise ValueError(
            f"script hash not allowlisted: {digest} — approve it on this host with "
            f"`vigil-agent allow-script` or add it to allowed_script_hashes in agent.yml")

    timeout = int(params.get("timeout", _EXEC_TIMEOUT))
    if timeout < 1 or timeout > 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")

    is_powershell = shell in ("powershell", "pwsh")
    tmp_dir = Path(tempfile.mkdtemp(prefix="vigil-script-"))
    try:
        script_path = tmp_dir / ("script.ps1" if is_powershell else "script.sh")
        script_path.write_text(scripthash.normalise(body), encoding="utf-8")
        if os.name == "posix":
            script_path.chmod(0o700)
        if is_powershell:
            cmd = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                   "Bypass", "-File", str(script_path)]
        else:
            cmd = [shell, str(script_path)]
        output = ex._run(cmd, timeout=timeout, extra_env=_input_env(inputs))
        return ActionOutput(f"[{digest}]\n{output}", {"exit_code": 0})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _execute_script(params: dict, config: AgentConfig, inputs: dict | None = None) -> str:
    if "script" in params:
        return _execute_inline_script(params, config, inputs)

    script_name = params.get("script_name", "")
    if not _SAFE_SCRIPT_NAME.match(script_name):
        raise ValueError(f"Invalid script name: {script_name!r}")

    scripts_dir = config.scripts_dir.resolve()
    script_path = (scripts_dir / script_name).resolve()

    # Path traversal protection. is_relative_to() rather than a string
    # startswith on str(scripts_dir) + "/": that hardcoded separator never
    # matches a Windows path, so every script was refused there — fail-safe,
    # but it meant execute_script could not work on Windows at all.
    if not script_path.is_relative_to(scripts_dir):
        raise ValueError(
            f"Script path escapes scripts directory: {script_name!r}"
        )

    if not script_path.is_file():
        raise ValueError(f"Script not found: {script_name}")

    # Whoever can change the script, or any directory above it up to
    # scripts_dir, chooses what runs here as root/LocalSystem (SEC-2).
    from ..scripttrust import untrusted
    problem = untrusted(script_path, scripts_dir)
    if problem:
        fix = (f"Run: chown -R root {scripts_dir} && chmod -R go-w {scripts_dir}"
               if os.name == "posix" else
               "Re-run the agent installer as an administrator; it locks the scripts folder.")
        raise ValueError(
            f"Refusing to run {script_name}: {problem}. {fix}"
        )

    return ActionOutput(
        ex._run([str(script_path)], extra_env=_input_env(inputs)),
        {"exit_code": 0},
    )


def _sanitize_notify_message(raw: str) -> str:
    """Cap at 200 chars and strip anything outside the conservative
    allowlist, before the text reaches a command line."""
    text = re.sub(r"[^A-Za-z0-9 .,:;!?()\-'']", "", raw)[:200]
    return " ".join(text.split())


def _notify_user(message: str) -> None:
    """Show the user a desktop notification. Any failure is logged and
    swallowed — a missing notify tool must never become a reboot-blocker."""
    if sys.platform == "win32":
        cmd = ["msg", "*", message]
    elif sys.platform == "darwin":
        cmd = ["osascript", "-e",
               'display notification "' + message + '" with title "Vigil"']
    else:
        cmd = ["notify-send", "Vigil", message]
    try:
        ex._run(cmd, timeout=15)
    except FileNotFoundError:
        if sys.platform != "darwin":
            try:
                ex._run(["wall", message], timeout=15)
            except Exception:
                logger.warning("Reboot notification failed: wall unavailable")
        else:
            logger.warning("Reboot notification failed: %s", "notify-send")
    except Exception:
        logger.warning("Reboot notification failed", exc_info=True)


def _reboot(params: dict, config: AgentConfig) -> str:
    delay = int(params.get("delay_seconds", 0))
    if delay < 0:
        raise ValueError("delay_seconds must be non-negative")
    defer_limit = int(params.get("defer_limit", 0))
    if not 0 <= defer_limit <= 8:
        raise ValueError("defer_limit must be between 0 and 8")
    defer_minutes = int(params.get("defer_minutes", 0))
    if defer_minutes and not 5 <= defer_minutes <= 240:
        raise ValueError("defer_minutes must be between 5 and 240")
    message = _sanitize_notify_message(str(params.get("notify_message", "")))

    task_id = str(params.get("task_id", ""))
    deferral = RebootDeferral(config.data_dir)
    deferral_active = bool(
        task_id and (defer_limit > 0 or defer_minutes > 0)
        and deferral.expiry_remaining() > 0
        and deferral.exhausted() is False
    )
    # A new task id resets the deferral budget; the same id keeps counting.
    if task_id:
        deferral.record(task_id, defer_limit, defer_minutes)
    # Capture exhaustion before clear(): a user who has used up every
    # deferral may no longer block the reboot, so it must be forced.
    force_close = defer_limit <= 0 or deferral.exhausted()
    if deferral.expired():
        deferral.clear()
        deferral_active = False

    if sys.platform == "win32":
        # Windows shutdown.exe: /t takes raw seconds.
        argv = ["shutdown", "/r", "/t", str(delay)]
        # -r +N on Linux takes MINUTES while /t takes SECONDS — do not
        # "tidy" the two into the same unit.
        if message:
            argv += ["/c", message]
        # /f forces apps closed. Only when no deferral is in play: a
        # fresh dispatch with a budget, or a dispatch inside an active
        # deferral window, must not rip the user's work away; but once
        # the budget is exhausted the user may no longer block it.
        if force_close:
            argv.append("/f")
    else:
        # coreutils shutdown: -r now / +N (minutes).
        if delay == 0:
            argv = ["shutdown", "-r", "now"]
        else:
            minutes = max(1, delay // 60)
            argv = ["shutdown", "-r", f"+{minutes}"]

    if params.get("notify") and message:
        _notify_user(message)
    output = ex._run(argv)
    deferral.clear()
    return ActionOutput(
        output, {"delay_seconds": delay, "deferral_active": bool(deferral_active)})


def _run_command(params: dict, config: AgentConfig) -> str:
    """Run an arbitrary command. full_control mode only (defense-in-depth)."""
    if config.mode != "full_control":
        raise ValueError("run_command is only available in full_control mode")

    command = params.get("command", "")
    if not command:
        raise ValueError("command is required")

    timeout = int(params.get("timeout", _EXEC_TIMEOUT))
    if timeout < 1 or timeout > 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")

    return ActionOutput(ex._run(shlex.split(command), timeout=timeout),
                        {"exit_code": 0})


def _set_hostname(params: dict, _config: AgentConfig) -> str:
    hostname = params.get("hostname", "")
    if not _SAFE_HOSTNAME.match(hostname):
        raise ValueError(f"Invalid hostname: {hostname!r}")
    return ActionOutput(
        ex._run(["hostnamectl", "set-hostname", hostname]), {"hostname": hostname})
