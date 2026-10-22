"""Power and sleep actions: timeouts, hibernation, plans, lid action, sleep/hibernate now.

The three platforms expose completely different surfaces — ``powercfg`` on
Windows, logind on Linux, ``pmset`` on macOS — so each handler dispatches on
``sys.platform`` and raises ``RuntimeError`` where the platform has no
equivalent. An unsupported action failing the step with a clear message beats
silently doing nothing: an operator who asked for a display timeout on Linux
should not be told it worked.

Every command runs through ``executor._run`` (no shell, raises on non-zero
exit), except sleep/hibernate now on Windows and macOS, which must outlive the
agent and are launched detached. The delay there exists so the task result
reaches the server *before* the machine suspends.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import AgentConfig
from ..executor import ActionOutput


class _Commands:
    """The command seam: forwards ``ex._run(...)`` to the live ``executor._run``.

    Import order forbids binding it any other way — importing this module from
    ``executor`` re-enters executor's own import of these handlers, so neither
    the module object nor its ``_run`` can be bound at import time. One call
    runs with no shell and raises on a non-zero exit; tests patch
    ``vigil_agent.executor._run``, which the seam resolves by name.
    """

    __slots__ = ()

    def __call__(self, cmd: list[str], **kwargs) -> str:
        from .. import executor

        return executor._run(list(cmd), **kwargs)

    def __getattr__(self, name: str):
        from .. import executor

        return getattr(executor, name)


ex = _Commands()


#: Where the Linux logind settings live. A module constant so tests can point
#: it at a temporary directory.
LOGIND_DROPIN = Path("/etc/systemd/logind.conf.d/60-vigil-power.conf")

MINUTES_MAX = 1440
DEFAULT_DELAY_SECONDS = 15

_PLANS = ("balanced", "high_performance", "power_saver")
_LID_ACTIONS = ("sleep", "hibernate", "shutdown", "nothing")

# powercfg takes numbers where logind takes words.
_POWERCFG_LID = {"nothing": "0", "sleep": "1", "hibernate": "2", "shutdown": "3"}
_LOGIND_LID = {"sleep": "suspend", "hibernate": "hibernate",
               "shutdown": "poweroff", "nothing": "ignore"}

_WIN_PLANS = {"balanced": "SCHEME_BALANCED",
              "high_performance": "SCHEME_MIN",
              "power_saver": "SCHEME_MAX"}
_LINUX_PLANS = {"balanced": "balanced",
                "high_performance": "performance",
                "power_saver": "power-saver"}

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}

# These exist only on Windows; getattr keeps this module importable elsewhere.
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x8)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


def _unsupported(name: str) -> RuntimeError:
    return RuntimeError(f"{name} is not supported on {_os_name()}")


def _os_name() -> str:
    return {"win32": "Windows", "linux": "Linux", "darwin": "macOS"}.get(
        sys.platform, sys.platform)


def _minutes(params: dict, key: str, default: int | None = None) -> int:
    """A whole number of minutes in 0..1440, or a ValueError.

    ``0`` means "never", so it is a legal value and must not be treated as an
    absent one; a missing key with no default is the caller's bug.
    """
    raw = params.get(key, default)
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"{key} must be a whole number of minutes from 0 to {MINUTES_MAX}")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"{key} must be a whole number of minutes from 0 to {MINUTES_MAX}") from None
    if not 0 <= value <= MINUTES_MAX:
        raise ValueError(f"{key} must be a whole number of minutes from 0 to {MINUTES_MAX}")
    return value


def _enabled(params: dict) -> bool:
    raw = params.get("enabled")
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("enabled must be true or false")


def _choice(params: dict, key: str, allowed: tuple[str, ...]) -> str:
    value = str(params.get(key, "")).strip().lower()
    if value not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(allowed)}")
    return value


def _delay(params: dict) -> int:
    raw = params.get("delay_seconds", DEFAULT_DELAY_SECONDS)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError("delay_seconds must be between 5 and 3600") from None
    if not 5 <= value <= 3600:
        raise ValueError("delay_seconds must be between 5 and 3600")
    return value


def _launch_detached(argv: list[str]) -> None:
    """Start a command that must survive this process — never via a shell."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def _powershell_sleep_then(command: str, delay: int) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"Start-Sleep -Seconds {delay}; {command}"]


# ── Timeouts ────────────────────────────────────────────────────────────────


def _set_sleep_timeout(params: dict, _config: AgentConfig) -> str:
    ac = _minutes(params, "ac_minutes")
    dc = _minutes(params, "dc_minutes", default=ac)

    note = ""
    if sys.platform == "win32":
        out = "\n".join(filter(None, [
            ex._run(["powercfg", "/change", "standby-timeout-ac", str(ac)]),
            ex._run(["powercfg", "/change", "standby-timeout-dc", str(dc)]),
        ]))
    elif sys.platform == "linux":
        note = "\nOn Linux the timeout is idle-based and ignores AC/DC."
        out = _logind_update(_idle_sleep_keys(ac))
    else:
        out = "\n".join(filter(None, [
            ex._run(["pmset", "-c", "sleep", str(ac)]),
            ex._run(["pmset", "-b", "sleep", str(dc)]),
        ]))
    return ActionOutput(
        (out + note).strip(), {"ac_minutes": ac, "dc_minutes": dc})


def _set_display_timeout(params: dict, _config: AgentConfig) -> str:
    if sys.platform == "linux":
        raise _unsupported("set_display_timeout")
    ac = _minutes(params, "ac_minutes")
    dc = _minutes(params, "dc_minutes", default=ac)

    if sys.platform == "win32":
        out = "\n".join(filter(None, [
            ex._run(["powercfg", "/change", "monitor-timeout-ac", str(ac)]),
            ex._run(["powercfg", "/change", "monitor-timeout-dc", str(dc)]),
        ]))
    else:
        out = "\n".join(filter(None, [
            ex._run(["pmset", "-c", "displaysleep", str(ac)]),
            ex._run(["pmset", "-b", "displaysleep", str(dc)]),
        ]))
    return ActionOutput(out, {"ac_minutes": ac, "dc_minutes": dc})


# ── logind drop-in (Linux) ──────────────────────────────────────────────────

_IDLE_OWNED = ("IdleAction", "IdleActionSec")
_LID_OWNED = ("HandleLidSwitch", "HandleLidSwitchExternalPower")


def _idle_sleep_keys(ac: int) -> dict[str, str]:
    if ac == 0:
        # "ignore" is logind's never. IdleActionSec must go with it: a stale
        # value would win the moment another tool set IdleAction again.
        return {"IdleAction": "ignore", "IdleActionSec": ""}
    return {"IdleAction": "suspend", "IdleActionSec": f"{ac}min"}


def _read_dropin(path: Path) -> dict[str, str]:
    """The ``[Login]`` keys in one drop-in, as written by logind's own parser:
    last file wins across the directory, last line wins inside a file."""
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    in_login = False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_login = stripped == "[Login]"
            continue
        if not in_login or not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        found[key.strip()] = value.strip()
    return found


def _logind_update(owned: dict[str, str]) -> str:
    """Merge our keys into the drop-in and make logind re-read it.

    Written atomically and sorted so a fleet's drops are diffable. Keys this
    action does not own survive: the other power action's in this file, and
    everything in the operator's own files, which are never touched.
    """
    path = Path(LOGIND_DROPIN)
    # Only Vigil's own file is read and rewritten. Copying another drop-in's
    # keys in here would freeze them: this file sorts later and would win over
    # any edit the operator makes to their own.
    keys: dict[str, str] = _read_dropin(path)
    # Only the keys this action owns are rewritten; an empty value removes one.
    for key, value in owned.items():
        if value == "":
            keys.pop(key, None)
        else:
            keys[key] = value

    lines = ["# Managed by Vigil (power task actions). Edits here are overwritten.",
             "", "[Login]"]
    lines += [f"{key}={keys[key]}" for key in sorted(keys)]
    body = "\n".join(lines) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w") as handle:
            handle.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    # A HUP makes logind re-read its config; restarting it would end sessions.
    ex._run(["systemctl", "kill", "-s", "HUP", "systemd-logind"])
    return f"Wrote {path}"


# ── Hibernation ─────────────────────────────────────────────────────────────


def _set_hibernate(params: dict, _config: AgentConfig) -> str:
    enabled = _enabled(params)

    if sys.platform == "win32":
        out = ex._run(["powercfg", "/hibernate", "on" if enabled else "off"])
    elif sys.platform == "linux":
        targets = ["hibernate.target", "hybrid-sleep.target"]
        out = ex._run(["systemctl", "unmask" if enabled else "mask", *targets])
    else:
        out = ex._run(["pmset", "-a", "hibernatemode", "3" if enabled else "0"])
    return ActionOutput(out, {"enabled": enabled})


# ── Power plan ──────────────────────────────────────────────────────────────


def _set_power_plan(params: dict, _config: AgentConfig) -> str:
    plan = _choice(params, "plan", _PLANS)

    if sys.platform == "win32":
        out = ex._run(["powercfg", "/setactive", _WIN_PLANS[plan]])
    elif sys.platform == "linux":
        if shutil.which("powerprofilesctl") is None:
            raise _unsupported("set_power_plan")
        out = ex._run(["powerprofilesctl", "set", _LINUX_PLANS[plan]])
    else:
        raise _unsupported("set_power_plan")
    return ActionOutput(out, {"plan": plan})


# ── Lid action ──────────────────────────────────────────────────────────────


def _set_lid_action(params: dict, _config: AgentConfig) -> str:
    ac = _choice(params, "ac", _LID_ACTIONS)
    dc = _choice(params, "dc", _LID_ACTIONS) if params.get("dc") not in (None, "") else ac

    if sys.platform == "win32":
        out = "\n".join(filter(None, [
            ex._run(["powercfg", "/setacvalueindex", "SCHEME_CURRENT",
                     "SUB_BUTTONS", "LIDACTION", _POWERCFG_LID[ac]]),
            ex._run(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT",
                     "SUB_BUTTONS", "LIDACTION", _POWERCFG_LID[dc]]),
            ex._run(["powercfg", "/setactive", "SCHEME_CURRENT"]),
        ]))
    elif sys.platform == "linux":
        out = _logind_update({"HandleLidSwitchExternalPower": _LOGIND_LID[ac],
                              "HandleLidSwitch": _LOGIND_LID[dc]})
    else:
        raise _unsupported("set_lid_action")
    return ActionOutput(out, {"ac": ac, "dc": dc})


# ── Sleep / hibernate now ───────────────────────────────────────────────────

_SUSPEND_PS = ("Add-Type -AssemblyName System.Windows.Forms; "
               "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)")


def _sleep_now(params: dict, _config: AgentConfig) -> str:
    delay = _delay(params)

    if sys.platform == "win32":
        _launch_detached(_powershell_sleep_then(_SUSPEND_PS, delay))
        out = f"Suspend scheduled in {delay}s"
    elif sys.platform == "linux":
        out = ex._run(["systemd-run", f"--on-active={delay}",
                       "--unit", "vigil-sleep-now", "systemctl", "suspend"])
    else:
        _launch_detached(["sh", "-c", f"sleep {delay}; pmset sleepnow"])
        out = f"Suspend scheduled in {delay}s"
    return ActionOutput(out, {"delay_seconds": delay})


def _hibernate_now(params: dict, _config: AgentConfig) -> str:
    if sys.platform == "darwin":
        raise _unsupported("hibernate_now")
    delay = _delay(params)

    if sys.platform == "win32":
        _launch_detached(_powershell_sleep_then("shutdown.exe /h", delay))
        out = f"Hibernate scheduled in {delay}s"
    else:
        out = ex._run(["systemd-run", f"--on-active={delay}",
                       "--unit", "vigil-hibernate-now", "systemctl", "hibernate"])
    return ActionOutput(out, {"delay_seconds": delay})
