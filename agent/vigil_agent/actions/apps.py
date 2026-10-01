"""App actions: install / upgrade / uninstall one app by its inventory identity (M7).

Moved out of executor.py. The identity is what the Apps page shows — a
``source`` plus the package id — so an id a reader copied off that page acts on
the thing they were looking at. ``source`` defaults to the host's primary
package manager; a primary-database source the host does not run (``rpm`` on a
dpkg host) has no command that could reach it and is refused, and so is every
Windows store.

Nothing on the wire is trusted: ``app`` and ``version`` may have been resolved
from a ``${{ … }}`` input after the server validated the spec, so the server's
two character rules are applied again here before an argv is built
(:func:`pkg_manager.validate_app_identifier`). ``_validate_package_name`` cannot
stand in for it — that one refuses ``-`` anywhere, which rules out ``libgl1``
and every Windows GUID, and says nothing about a leading ``-`` (an option) or an
embedded space.

The snap and flatpak argv goes through ``pkg_manager._run``, which raises on a
non-zero exit and runs with a cleaned environment; the primary-manager calls go
through the ``PackageManager`` methods, which raise the same way.

After a step that changed anything the agent re-collects its software list, so
the next check-in reports the new state. A failed collection is logged, not
raised: the change is on the host either way, and the next scheduled collection
reports it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from .. import pkg_manager, software
from ..config import AgentConfig
from ..executor import ActionOutput, logger


_PRIMARY_SOURCES = frozenset({"dpkg", "rpm", "apk", "pacman"})
_STORE_SOURCES = frozenset({"snap", "flatpak"})
_WINDOWS_SOURCES = frozenset({"winget", "chocolatey", "scoop", "registry"})
#: The inventory names each manager's packages by its database, not its CLI:
#: apt installs show up as ``dpkg``, dnf/yum/zypper as ``rpm``. ``source`` in a
#: task and in the outputs uses the inventory's name, as the Apps page does.
_MANAGER_SOURCE = {"apt": "dpkg", "apt-get": "dpkg", "dnf": "rpm", "yum": "rpm",
                   "zypper": "rpm", "apk": "apk", "pacman": "pacman"}
#: The Windows sources whose commands are the package manager's own; the
#: registry is the unmanaged remainder and can only ever be removed.
_MANAGER_SOURCES_WINDOWS = frozenset({"winget", "chocolatey"})

#: An install/upgrade/uninstall is a download plus an installer: 15 minutes.
_WINDOWS_TIMEOUT = 900
#: winget's own messages are the useful half of a failure; the rest of its
#: output is a progress-bar history.
_WINGET_TAIL_CHARS = 300
#: MSI/choco: the last two mean "removed, but reboot to finish" — a success.
_REBOOT_CODES = frozenset({1641, 3010})
#: msiexec 1605: this product is not installed. The removal did what it could.
_MSI_NOT_INSTALLED = 1605
#: winget ``0x8A15002B`` — nothing to upgrade, the installed version is current.
WINGET_NO_APPLICABLE_UPDATE = 0x8A15002B
#: winget ``0x8A150061`` — the requested package (at that version) is installed.
WINGET_PACKAGE_ALREADY_INSTALLED = 0x8A150061
#: The exit codes each winget action accepts (0 included). Both spellings of an HRESULT are
#: compared by :func:`_code_is`, so these are the unsigned forms.
_WINGET_OK_UPGRADE = frozenset({0, WINGET_NO_APPLICABLE_UPDATE})
_WINGET_OK_INSTALL = frozenset({0, WINGET_PACKAGE_ALREADY_INSTALLED})
#: winget ``0x8A150063`` from ``pin remove``: the id has no pin. The app is
#: free to upgrade either way, which is what an unpin asked for.
WINGET_NO_PIN = 0x8A150063
_WINGET_OK_UNPIN = frozenset({0, WINGET_NO_PIN})
#: dnf's words when the versionlock plugin is not installed.
_VERSIONLOCK_MISSING = re.compile(r"no such command:? ?'?versionlock", re.IGNORECASE)
#: A Windows Installer product code, as one registry key name.
_MSI_PRODUCT_CODE = re.compile(r"^\{[0-9A-Fa-f-]{36}\}$")
#: Same GUID anywhere inside an ``UninstallString``.
_MSI_GUID_IN_STRING = re.compile(r"\{[0-9A-Fa-f-]{36}\}")


def _inventory_source(manager: pkg_manager.PackageManager) -> str:
    return _MANAGER_SOURCE.get(manager.name, manager.name)


def _app_param(params: dict, name: str) -> str:
    """The validated value of ``app`` or ``version``, refused otherwise."""
    text = "" if params.get(name) in (None, "") else str(params[name])
    if not text:
        raise RuntimeError(f"{name} is required")
    if not pkg_manager.validate_app_identifier(text, version=name == "version"):
        raise RuntimeError(f"{name} {text!r} is not a valid app identifier")
    return text


def _primary(pm: pkg_manager.PackageManager | None) -> pkg_manager.PackageManager:
    if pm is None:
        raise RuntimeError("No supported package manager found")
    return pm


def _resolve_source(params: dict, pm: pkg_manager.PackageManager | None) -> str:
    """Which source to act on, refusing what this host cannot serve."""
    requested = params.get("source")
    if requested in (None, ""):
        return _inventory_source(_primary(pm))
    if requested in _WINDOWS_SOURCES:
        raise RuntimeError(f"source {requested} is Windows-only")
    if requested not in _PRIMARY_SOURCES | _STORE_SOURCES:
        raise RuntimeError(f"unknown source {requested!r}")
    manager = _primary(pm)
    if requested in _PRIMARY_SOURCES and requested != _inventory_source(manager):
        raise RuntimeError(
            f"source {requested} is not this host's package manager "
            f"({manager.name}, source {_inventory_source(manager)})")
    return requested


def _install_primary(pm: pkg_manager.PackageManager, app: str,
                     version: str) -> None:
    """Install through the host's manager, pinning *version* when given.

    Pin syntax differs per manager: apt, zypper and apk take ``name=version``,
    dnf and yum the RPM-style ``name-version``. pacman has no way to name one
    version non-interactively, so a pin there is refused rather than quietly
    dropped.
    """
    if not version:
        pm.install(app)
        return
    if pm.name in ("apt", "apt-get", "zypper", "apk"):
        spec = f"{app}={version}"
    elif pm.name in ("dnf", "yum"):
        spec = f"{app}-{version}"
    else:
        raise RuntimeError(f"version pinning is not supported for {pm.name}")
    # app and version were each validated by _app_param; pm.install() checks the
    # assembled spec again with _validate_package_name (which allows = and ~).
    pm.install(spec)


def _store_command(store: str, action: str, app: str = "") -> list[str]:
    """The argv for one snap/flatpak operation."""
    if store == "snap":
        verb = {"install": "install", "upgrade": "refresh",
                "uninstall": "remove"}[action]
        return ["snap", verb] + ([app] if app else [])
    if action == "install":
        return ["flatpak", "install", "-y", "--noninteractive", "flathub", app]
    verb = "update" if action in ("upgrade", "upgrade_all") else "uninstall"
    argv = ["flatpak", verb, "-y", "--noninteractive"]
    return argv + [app] if app else argv


def _run_windows(command: list[str] | str, timeout: int = _WINDOWS_TIMEOUT,
                 env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run one Windows package command, returning ``(exit code, output)``.

    One seam for the whole Windows half, so the tests substitute it once.
    *command* may also be a command **string**: on Windows ``subprocess`` hands
    a string straight to ``CreateProcess`` with ``shell=False``, so a registry
    ``QuietUninstallString`` is split by the same rules as any other program's
    command line, with no shell in between to reinterpret it.
    """
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
        env=env if env is not None else pkg_manager.clean_env(),
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _tail(text: str) -> str:
    """The tool's own last line.

    winget prints a progress history and puts its message — "No installed
    package found matching input criteria" — at the end, so the head of the
    output says nothing about why it stopped.
    """
    tail = (text or "").strip()[-_WINGET_TAIL_CHARS:]
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _code_is(code: int, wanted: int) -> bool:
    """Whether *code* is *wanted*, in its signed or its unsigned spelling.

    winget's HRESULTs reach us negative from some process reports and positive
    (``code & 0xFFFFFFFF``) from others; the two spellings are one condition.
    """
    return code == wanted or (code & 0xFFFFFFFF) == wanted


def _check_exit(code: int, output: str, program: str,
                ok_codes: frozenset[int]) -> None:
    """Raise with the tool's own last line unless *code* is one we accept."""
    if not any(_code_is(code, ok) for ok in ok_codes):
        raise RuntimeError(f"{program} exited {code}: {_tail(output)}")


def _reboot(code: int) -> bool:
    """Whether an exit code means "done, but reboot to finish"."""
    return _code_is(code, 1641) or _code_is(code, 3010)


def _winget_command(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """winget's argv plus the environment its packaged loader needs.

    ``winget.exe`` under ``%ProgramFiles%\\WindowsApps`` cannot find its VCLibs
    C runtime when a service launches it directly; ``winget_env`` puts the
    runtime on PATH (see ``pkg_manager.winget_env``).
    """
    binary, _reason = pkg_manager.resolve_winget()
    if not binary:
        raise RuntimeError(
            "winget is not available on this host (App Installer absent, or "
            "this account cannot reach it)")
    return [binary, *argv], pkg_manager.winget_env(binary)


def _run_winget(argv: list[str],
                ok_codes: frozenset[int] = frozenset({0})) -> int:
    """One winget command, accepting the exit codes this action expects."""
    command, env = _winget_command(argv)
    code, output = _run_windows(command, timeout=_WINDOWS_TIMEOUT, env=env)
    _check_exit(code, output, "winget", ok_codes)
    return code


def _run_choco(argv: list[str]) -> int:
    """One Chocolatey command. 1641/3010 mean "done — reboot to finish"."""
    code, output = _run_windows(argv, timeout=_WINDOWS_TIMEOUT)
    _check_exit(code, output, "choco", frozenset({0}) | _REBOOT_CODES)
    return code


def _windows_source(params: dict) -> str:
    """Which Windows source to act on.

    winget wins when it resolves — it is machine-wide and the resolver finds it
    even off a service account's PATH — then Chocolatey. Registry rows are not
    a source an install or upgrade can use, so they are refused here and
    handled by the uninstall path.
    """
    requested = params.get("source")
    if requested in (None, ""):
        if pkg_manager.resolve_winget()[0]:
            return "winget"
        if shutil.which("choco") is not None:
            return "chocolatey"
        raise RuntimeError("no package manager on this host (winget / Chocolatey)")
    if requested in _PRIMARY_SOURCES:
        raise RuntimeError(f"source {requested} is Linux-only")
    if requested == "scoop":
        raise RuntimeError("Scoop installs are per-user; Vigil does not manage "
                           "them in this release")
    if requested in _STORE_SOURCES:
        raise RuntimeError(f"source {requested} is Linux-only")
    if requested not in _MANAGER_SOURCES_WINDOWS | {"registry"}:
        raise RuntimeError(f"unknown source {requested!r}")
    return requested


def _windows_uninstall_target(app: str) -> dict:
    """The machine-wide Uninstall registry row whose key is *app*.

    *app* is the id the inventory reports for an unmanaged app — its registry
    key name — so that is what the Apps page hands back to us.
    """
    rows = software._read_uninstall_keys()
    wanted = app.lower()
    matches = [row for row in rows if (row.get("key") or "").lower() == wanted]
    for row in matches:
        if row.get("hive") == "HKLM":
            return row
    if any(row.get("hive") == "HKU" for row in matches):
        raise RuntimeError(
            f"{app} is a per-user install; Vigil does not uninstall per-user "
            "apps in this release")
    raise RuntimeError(f"no machine-wide Uninstall entry {app}")


def _is_msi_row(row: dict) -> bool:
    """Whether Windows Installer owns this install.

    ``WindowsInstaller`` is the documented flag; an ``MsiExec`` uninstall string
    is what the registry actually carries when the flag is missing, so either
    one routes the removal through ``msiexec``.
    """
    if str(row.get("WindowsInstaller")).strip() == "1":
        return True
    return (row.get("UninstallString") or "").strip().lower().startswith("msiexec")


def _msi_product_code(row: dict) -> str:
    """The product code for ``msiexec /x``: the key name when it is one, else
    the GUID inside ``UninstallString``."""
    key = (row.get("key") or "").strip()
    if _MSI_PRODUCT_CODE.fullmatch(key):
        return key
    found = _MSI_GUID_IN_STRING.search(row.get("UninstallString") or "")
    if found:
        return found.group(0)
    raise RuntimeError(
        f"{(row.get('DisplayName') or '').strip()} is a Windows Installer "
        f"product with no product code to uninstall ({key})")


def _quiet_program(quiet: str) -> str:
    """The program of a ``QuietUninstallString``, for its error string."""
    head = quiet.split('"')[1] if quiet.startswith('"') else quiet.split()[0]
    return head.replace("\\", "/").rsplit("/", 1)[-1]


def _upgrade_windows(params: dict, app: str) -> str:
    """``app_upgrade`` on Windows, for one app or for every outdated one."""
    source = _windows_source(params)
    if source == "registry":
        raise RuntimeError(
            "source registry is the inventory's unmanaged remainder: no "
            "package manager can upgrade it — uninstall it and install the "
            "newer version with winget or Chocolatey")
    if not app:
        before = _outdated(_recollect())
        if source == "winget":
            code = _run_winget(
                ["upgrade", "--all", "--silent", "--accept-package-agreements",
                 "--accept-source-agreements", "--disable-interactivity"],
                _WINGET_OK_UPGRADE)
        else:
            code = _run_choco(["choco", "upgrade", "all", "-y", "--no-progress"])
        after = _outdated(_recollect())
        upgraded = sum(1 for key, latest in before.items()
                       if after.get(key) != latest)
        reboot = " (a reboot is needed to finish)" if _reboot(code) else ""
        return ActionOutput(f"upgraded {upgraded} apps{reboot}",
                            {"upgraded": upgraded, "failed": 0})
    if source == "winget":
        code = _run_winget(
            ["upgrade", "--id", app, "--exact", "--silent",
             "--accept-package-agreements", "--accept-source-agreements",
             "--disable-interactivity"], _WINGET_OK_UPGRADE)
    else:
        code = _run_choco(["choco", "upgrade", app, "-y", "--no-progress"])
    # winget's "no applicable upgrade" is the same outcome the re-collection
    # would report: nothing moved, which is upgraded 0 rather than a failure.
    if _code_is(code, WINGET_NO_APPLICABLE_UPDATE):
        return ActionOutput(f"{app} is already at its latest version",
                            {"upgraded": 0, "failed": 0})
    reboot = " (a reboot is needed to finish)" if _reboot(code) else ""
    return ActionOutput(f"upgraded {app} from {source}{reboot}",
                        {"upgraded": 1, "failed": 0})


def _registry_uninstall(app: str) -> bool:
    """Silently remove one registry (unmanaged) install.

    Returns whether the uninstaller asked for a reboot. Never runs a plain
    ``UninstallString``: that is an interactive wizard, and launched from a
    service it would sit on the console until the task timed out, having
    removed nothing.
    """
    row = _windows_uninstall_target(app)
    display = (row.get("DisplayName") or "").strip() or app
    if _is_msi_row(row):
        code, output = _run_windows(
            ["msiexec.exe", "/x", _msi_product_code(row), "/qn", "/norestart"],
            timeout=_WINDOWS_TIMEOUT)
        if _code_is(code, _MSI_NOT_INSTALLED):
            # Nothing to remove: the re-collection decides what "removed" means.
            return False
        _check_exit(code, output, "msiexec", frozenset({0}) | _REBOOT_CODES)
        return _code_is(code, 1641) or _code_is(code, 3010)
    quiet = (row.get("QuietUninstallString") or "").strip()
    if quiet:
        code, output = _run_windows(quiet, timeout=_WINDOWS_TIMEOUT)
        _check_exit(code, output, _quiet_program(quiet), frozenset({0}))
        return False
    raise RuntimeError(
        f"{display} has no silent uninstaller (only an interactive "
        "UninstallString) — remove it by hand or bring it under a package "
        "manager")


def _outdated(payload: dict) -> dict[str, str]:
    """``{"source|id": latest}`` for each item the collection says is outdated."""
    return {f"{item['source']}|{item['id']}": str(item["latest"])
            for item in payload.get("items", []) if item.get("latest")}


def _field_in_collection(payload: dict, source: str, ident: str,
                         field: str) -> str:
    """One field of the collection item with this identity, or ``""``."""
    for item in payload.get("items", []):
        if item.get("source") == source and item.get("id") == ident:
            return str(item.get(field) or "")
    return ""


def _recollect() -> dict:
    """Re-collect so the next check-in ships the new state; ``{}`` if that fails.

    ``collect_now`` also refreshes the pending payload the check-in sends. A
    failure here is not the action's failure — the change already happened on
    the host, and the next scheduled collection reports it.
    """
    try:
        return software.collect_now()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("software re-collection after an app action failed: %s",
                       exc)
        return {}


def _app_install(params: dict, _config: AgentConfig) -> str:
    app = _app_param(params, "app")
    version = _app_param(params, "version") if params.get("version") else ""
    if sys.platform == "win32":
        source, code = _install_windows(params, app, version)
        after = _recollect()
        installed = _field_in_collection(after, source, app, "version")
        reboot = " (a reboot is needed to finish)" if _reboot(code) else ""
        return ActionOutput(
            f"installed {app} from {source}{reboot}",
            {"installed_version": installed, "source": source})
    pm = pkg_manager.detect()
    source = _resolve_source(params, pm)
    if source in _STORE_SOURCES:
        if version:
            raise RuntimeError(f"version pinning is not supported for {source}")
        pkg_manager._run(_store_command(source, "install", app))
    else:
        manager = _primary(pm)
        manager.refresh()
        _install_primary(manager, app, version)
    after = _recollect()
    return ActionOutput(
        f"installed {app} from {source}",
        {"installed_version": _field_in_collection(after, source, app, "version"),
         "source": source})


def _install_windows(params: dict, app: str, version: str) -> tuple[str, int]:
    """Install with the source the task named, else the host's own manager."""
    source = _windows_source(params)
    if source == "registry":
        raise RuntimeError(
            "source registry is the inventory's unmanaged remainder: nothing "
            "installs from it — install the app with winget or Chocolatey")
    if source == "winget":
        argv = ["install", "--id", app, "--exact", "--silent", "--scope",
                "machine", "--accept-package-agreements",
                "--accept-source-agreements", "--disable-interactivity"]
        if version:
            argv += ["--version", version]
        code = _run_winget(argv, _WINGET_OK_INSTALL)
    else:
        argv = ["choco", "install", app, "-y", "--no-progress"]
        if version:
            argv += ["--version", version]
        code = _run_choco(argv)
    return source, code


def _app_upgrade(params: dict, _config: AgentConfig) -> str:
    app = _app_param(params, "app") if params.get("app") else ""
    if sys.platform == "win32":
        return _upgrade_windows(params, app)
    pm = pkg_manager.detect()
    source = _resolve_source(params, pm)
    if not app:
        # Two collections bracket the upgrade: an item outdated before that is
        # not outdated afterwards is one the upgrade actually moved.
        before = _outdated(_recollect())
        if source in _STORE_SOURCES:
            pkg_manager._run(_store_command(source, "upgrade"))   # snap refresh / flatpak update
        else:
            manager = _primary(pm)
            manager.refresh()
            manager.upgrade_all()
        after = _outdated(_recollect())
        upgraded = sum(1 for key, latest in before.items()
                       if after.get(key) != latest)
        return ActionOutput(f"upgraded {upgraded} apps",
                            {"upgraded": upgraded, "failed": 0})
    if source in _STORE_SOURCES:
        pkg_manager._run(_store_command(source, "upgrade", app))
    else:
        manager = _primary(pm)
        manager.refresh()
        # "install" is the upgrade form on every primary manager: it moves the
        # named package to the candidate version and no-ops when it is current.
        manager.install(app)
    return ActionOutput(f"upgraded {app} from {source}",
                        {"upgraded": 1, "failed": 0})


#: A registry Uninstall key name: anything but a backslash or a control
#: character (the VM's own "Oracle VirtualBox Guest Additions" has spaces).
#: Safe to be this loose because the key is only compared with registry rows —
#: the command that runs comes from the matched row, never from this text.
_REGISTRY_KEY = re.compile(r"^[^\\\x00-\x1f]{1,255}$")


def _app_uninstall(params: dict, _config: AgentConfig) -> str:
    if params.get("source") == "registry":
        app = str(params.get("app") or "")
        if not _REGISTRY_KEY.fullmatch(app):
            raise RuntimeError(f"app {app!r} is not a valid registry key name")
    else:
        app = _app_param(params, "app")
    if sys.platform == "win32":
        return _uninstall_windows(params, app)
    pm = pkg_manager.detect()
    source = _resolve_source(params, pm)
    if source in _STORE_SOURCES:
        pkg_manager._run(_store_command(source, "uninstall", app))
    else:
        # No refresh(): a removal needs no package lists, and an unreachable
        # repository must not stop a package coming off the host.
        _primary(pm).remove(app)
    after = _recollect()
    removed = _field_in_collection(after, source, app, "version") == ""
    return ActionOutput(
        f"uninstalled {app} from {source}" if removed
        else f"{app} is still listed after uninstalling",
        {"removed": removed})


def _uninstall_windows(params: dict, app: str) -> str:
    """``app_uninstall`` on Windows: a manager's own uninstall, or the registry's."""
    source = _windows_source(params)
    reboot = False
    if source == "registry":
        reboot = _registry_uninstall(app)
    elif source == "winget":
        code = _run_winget(["uninstall", "--id", app, "--exact", "--silent",
                            "--disable-interactivity"])
        reboot = _reboot(code)
    else:
        code = _run_choco(["choco", "uninstall", app, "-y"])
        reboot = _reboot(code)
    after = _recollect()
    removed = _field_in_collection(after, source, app, "version") == ""
    reboot_text = " (a reboot is needed to finish)" if reboot else ""
    return ActionOutput(
        (f"uninstalled {app} from {source}" if removed
         else f"{app} is still listed after uninstalling") + reboot_text,
        {"removed": removed})


# ── app_pin ───────────────────────────────────────────────────────────────────

#: Where a hold lives that no command of ours can reach, per source.
_PIN_REFUSALS = {
    "pacman": "pacman holds live in pacman.conf IgnorePkg — edit it by hand",
    "scoop": "scoop cannot be pinned",
    "registry": "registry cannot be pinned",
}


def _windows_pin_source(params: dict) -> str:
    """The requested Windows source, checked for a pin.

    A source the pin cannot hold is refused in the pin's own words rather than
    by whichever rule the install path happens to reach first — the difference
    matters to whoever reads the failure: it is the pin that is unsupported
    here, not the app or the source.
    """
    requested = params.get("source")
    if requested in _PIN_REFUSALS:
        raise RuntimeError(_PIN_REFUSALS[requested])
    return _windows_source(params)


def _pin_commands(source: str, version: str, unpin: bool) -> list[list[str]]:
    """What holding or releasing an app on this source costs, in commands.

    A pin at a version is two commands wherever the hold is a thing of its own
    — install it, then hold it, in that order — and one everywhere else. An
    unpin is always one. Sources with no hold of their own are refused here,
    before any command runs, and so is asking snap or flatpak for a version
    they cannot pin.
    """
    if source in _PIN_REFUSALS:
        raise RuntimeError(_PIN_REFUSALS[source])
    if unpin:
        return [["unhold"]]
    if source in ("snap", "flatpak") and version:
        raise RuntimeError(f"{source} cannot pin a version")
    # Where the hold is a thing of its own, the version has to arrive before it
    # does; a store's pin is the version itself, and apk's hold *is* a
    # constraint.
    if source in ("dpkg", "rpm") and version:
        return [["install"], ["hold"]]
    return [["hold"]]


def _pin_argv(step: list[str], source: str, app: str, version: str,
              manager: str) -> list[str]:
    """The real argv for one placeholder step of a pin.

    ``manager`` is the command the host's package manager answers to — the rpm
    source of a RHEL host is yum there, and of a SUSE one zypper.
    """
    if step == ["install"]:
        return _pin_install_argv(manager, app, version)
    if step == ["unhold"]:
        return _unpin_argv(source, app, manager)
    return _pin_hold_argv(source, app, version, manager)


def _pin_hold_argv(source: str, app: str, version: str,
                   manager: str) -> list[str]:
    """The command that holds *app* on this source, at *version* if given.

    Only winget and Chocolatey name a version when they hold; apk's hold *is* a
    version constraint. Everywhere else the hold is its own thing, and where a
    version came with it the install step put it there first. The rpm hold is
    asked of the host's own manager — versionlock under dnf and yum, zypper's
    own lock on SUSE.
    """
    if source == "snap":
        return ["snap", "refresh", "--hold=forever", app]
    if source == "flatpak":
        return ["flatpak", "mask", app]
    if source == "winget":
        argv = ["pin", "add", "--id", app, "--exact", "--blocking",
                "--accept-source-agreements", "--disable-interactivity"]
        return argv + (["--version", version] if version else [])
    if source == "chocolatey":
        argv = ["choco", "pin", "add", f"-n={app}"]
        return argv + ([f"--version={version}"] if version else [])
    if source == "apk":
        # apk has no hold of its own: the pin is the version constraint, and
        # the version to freeze at is the caller's or the host's own.
        if not version:
            raise RuntimeError(
                "apk pins by version and this host reports no version for "
                f"{app} — give one, or install the app first")
        return ["apk", "add", f"{app}={version}"]
    if manager == "zypper":
        return ["zypper", "--non-interactive", "addlock", app]
    if source == "rpm":
        return [manager, "versionlock", "add", app]
    return ["apt-mark", "hold", app]


def _unpin_argv(source: str, app: str, manager: str) -> list[str]:
    """The command that releases *app* on this source."""
    if source == "snap":
        return ["snap", "refresh", "--unhold", app]
    if source == "flatpak":
        return ["flatpak", "mask", "--remove", app]
    if source == "winget":
        return ["pin", "remove", "--id", app, "--exact",
                "--disable-interactivity"]
    if source == "chocolatey":
        return ["choco", "pin", "remove", f"-n={app}"]
    if source == "apk":
        # A plain install drops the version constraint the pin put there.
        return ["apk", "add", app]
    if manager == "zypper":
        return ["zypper", "--non-interactive", "removelock", app]
    if source == "rpm":
        return [manager, "versionlock", "delete", app]
    return ["apt-mark", "unhold", app]


def _pin_install_argv(manager: str, app: str, version: str) -> list[str]:
    """The install that comes before a hold.

    Only the primary sources have a hold apart from the install, and each spells
    a version its own way: apt with ``=``, the rpm managers with a dash.
    """
    if manager in ("apt", "apt-get", "zypper"):
        spec = f"{app}={version}"
    else:
        spec = f"{app}-{version}"
    if manager == "zypper":
        # zypper takes --non-interactive before the subcommand, like its locks.
        return ["zypper", "--non-interactive", "install", "-y", spec]
    # The same quiet flag each manager's own install uses in pkg_manager.
    quiet = "-qq" if manager in ("apt", "apt-get") else "--quiet"
    return [manager, "install", "-y", quiet, spec]


def _require_versionlock(pm: pkg_manager.PackageManager) -> None:
    """Fail unless this host's dnf-family manager can versionlock.

    versionlock is a plugin, not part of dnf: on a RHEL-family host without it
    the subcommand does not exist, and dnf's own "No such command" is not
    something an operator can act on. Asking before running anything also keeps
    such a host from installing a version it then cannot hold.
    """
    try:
        help_text = pkg_manager._run([pm.name, "versionlock", "--help"])
    except RuntimeError as exc:
        raise _no_versionlock() from exc
    if _VERSIONLOCK_MISSING.search(help_text):
        raise _no_versionlock()


def _no_versionlock() -> RuntimeError:
    return RuntimeError("dnf versionlock plugin missing — install "
                        "python3-dnf-plugin-versionlock")


def _app_pin(params: dict, _config: AgentConfig) -> str:
    app = _app_param(params, "app")
    version = _app_param(params, "version") if params.get("version") else ""
    unpin = params.get("unpin")
    if unpin is not None and not isinstance(unpin, bool):
        raise RuntimeError(f"unpin must be a boolean, got {unpin!r}")
    unpin = bool(unpin)
    if unpin and version:
        raise RuntimeError("unpin takes no version")
    windows = sys.platform == "win32"
    if windows:
        source = _windows_pin_source(params)
    else:
        pm = pkg_manager.detect()
        source = _resolve_source(params, pm)
        manager = _primary(pm).name
        # apk has no hold of its own: the pin is a version constraint, and an
        # unpinned app's own version is what freezing it means.
        if source == "apk" and not version and not unpin:
            version = _installed_version(pm, app)
    # Everything a source cannot do — hold at all, or hold a version — is
    # refused here, before the first command is run.
    commands = _pin_commands(source, version, unpin)
    if windows:
        for step in commands:
            argv = _pin_argv(step, source, app, version, "")
            if source == "winget":
                _run_winget(argv, _WINGET_OK_UNPIN if unpin else frozenset({0}))
            else:
                _run_choco(argv)
    else:
        if source == "rpm":
            _require_versionlock(_primary(pm))
        for step in commands:
            pkg_manager._run(_pin_argv(step, source, app, version, manager))
    after = _recollect()
    pinned_version = (_field_in_collection(after, source, app, "version")
                      or version)
    return ActionOutput(
        f"{'released' if unpin else 'pinned'} {app} on {source}"
        + (f" at {pinned_version}" if pinned_version and not unpin else ""),
        {"pinned": not unpin, "pinned_version": pinned_version})


def _installed_version(pm: pkg_manager.PackageManager | None, app: str) -> str:
    """The version this host's manager reports for *app*, or ""."""
    if pm is None:
        return ""
    try:
        return pm.installed_version(app)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.debug("app_pin: %s reported no version for %s: %s",
                     pm.name, app, exc)
        return ""


# ── app_install_custom ────────────────────────────────────────────────────────
#
# The one app action that runs an installer no package manager vouches for, so
# every rule the server applied is applied again here before a byte is fetched,
# and the file runs only if its SHA-256 is exactly the one signed into the task.

_CUSTOM_KINDS = ("msi", "exe", "deb", "rpm")
_CUSTOM_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CUSTOM_ARGS = re.compile(r'^[A-Za-z0-9 /=_.:,@+"-]{0,300}$')
_CUSTOM_MAX_BYTES = 2 * 1024 ** 3
_CUSTOM_TIMEOUT = 1800


def _custom_kind(url: str, named: str) -> str:
    """The installer kind: the one named, else the URL path's extension."""
    tail = url.split("#", 1)[0].split("?", 1)[0].rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    from_url = ext if ext in _CUSTOM_KINDS else ""
    if named:
        if named not in _CUSTOM_KINDS:
            raise RuntimeError(f"kind must be one of {', '.join(_CUSTOM_KINDS)}, got {named!r}")
        if from_url and from_url != named:
            raise RuntimeError(f"kind {named} does not match the URL's .{from_url}")
        return named
    if not from_url:
        raise RuntimeError("the URL has no .msi, .exe, .deb or .rpm extension — say kind")
    return from_url


def _custom_download(url: str, dest_dir: Path, kind: str) -> Path:
    """Stream *url* into a new file under *dest_dir*; delete it on any failure."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(dest_dir, 0o700)
    fd, name = tempfile.mkstemp(dir=dest_dir, prefix=".vigil-installer-", suffix="." + kind)
    path = Path(name)
    try:
        resp = requests.get(url, timeout=(10, 300), stream=True)
        resp.raise_for_status()
        written = 0
        with os.fdopen(fd, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                written += len(chunk)
                if written > _CUSTOM_MAX_BYTES:
                    raise RuntimeError("download exceeds 2 GiB")
                fh.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _custom_run(path: Path, kind: str, args: str) -> str:
    """Install the verified file silently; returns a note ("" or a reboot hint)."""
    if kind == "msi":
        code, output = _run_windows(["msiexec.exe", "/i", str(path), "/qn", "/norestart"],
                                    timeout=_CUSTOM_TIMEOUT)
        _check_exit(code, output, "msiexec", frozenset({0}) | _REBOOT_CODES)
        return " (reboot required)" if _reboot(code) else ""
    if kind == "exe":
        code, output = _run_windows([str(path), *args.split()], timeout=_CUSTOM_TIMEOUT)
        _check_exit(code, output, path.name, frozenset({0, 3010}))
        return " (reboot required)" if _code_is(code, 3010) else ""
    if kind == "deb":
        argv = (["apt-get", "install", "-y", str(path)] if shutil.which("apt-get")
                else ["dpkg", "-i", str(path)])
    else:
        tool = next((t for t in ("dnf", "yum") if shutil.which(t)), None)
        argv = [tool, "install", "-y", str(path)] if tool else ["rpm", "-i", str(path)]
    pkg_manager._run(argv, timeout=_CUSTOM_TIMEOUT)
    return ""


def _app_install_custom(params: dict, config: AgentConfig) -> str:
    url = str(params.get("url") or "")
    if not url.startswith("https://") or len(url) > 2000 or any(c.isspace() for c in url):
        raise RuntimeError("url must be an https URL of at most 2000 characters, no spaces")
    expected = str(params.get("sha256") or "").lower()
    if not _CUSTOM_SHA256.fullmatch(expected):
        raise RuntimeError("sha256 must be 64 hexadecimal characters")
    kind = _custom_kind(url, str(params.get("kind") or ""))
    args = str(params.get("args") or "")
    if args and (kind != "exe" or not _CUSTOM_ARGS.fullmatch(args)):
        raise RuntimeError("args is a silent-install switch list, only for kind exe")
    app = str(params.get("app") or "")
    if app and not pkg_manager.validate_app_identifier(app):
        raise RuntimeError(f"app {app!r} is not a valid app identifier")
    windows = sys.platform == "win32"
    if windows != (kind in ("msi", "exe")):
        raise RuntimeError(f"a .{kind} installer does not run on this platform")

    path = _custom_download(url, Path(config.data_dir) / "downloads", kind)
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError(f"installer failed SHA-256 verification: expected {expected}, got {actual}")
        note = _custom_run(path, kind, args)
    finally:
        path.unlink(missing_ok=True)

    installed = ""
    if app:
        after = _recollect()
        installed = next((str(i.get("version") or "") for i in after.get("items", [])
                          if i.get("id") == app), "")
    else:
        _recollect()
    return ActionOutput(f"installed {kind} from {url} (sha256 verified){note}",
                        {"sha256": actual, "installed_version": installed})


# ── app_ensure ────────────────────────────────────────────────────────────────
#
# The one-rule form a policy compiles to: "this app should be present / at its
# latest / pinned at X / absent here". It reads the host's own inventory first
# and does only what that gap needs, through the same handlers above, so
# running it twice changes nothing the second time.

_ENSURE_STATES = frozenset({"present", "latest", "pinned", "absent"})


def _ensure_row(payload: dict, app: str, source: str) -> dict | None:
    """The inventory item the rule names — same id, and the same source when
    the rule gives one. A row whose version is not the wanted one wins over
    one that is, so a host with two copies is never called compliant early."""
    rows = [item for item in payload.get("items", [])
            if item.get("id") == app and (not source or item.get("source") == source)]
    rows.sort(key=lambda i: (str(i.get("source") or ""), str(i.get("version") or "")))
    return rows[0] if rows else None


def _ensure_decision(state: str, row: dict | None, version: str) -> str:
    """none | install | upgrade | pin | uninstall — the policy decision table."""
    if row is None:
        if state == "absent":
            return "none"
        return "pin" if state == "pinned" else "install"
    have = str(row.get("version") or "")
    latest = str(row.get("latest") or "")
    if state == "absent":
        return "uninstall"
    if state == "latest" and latest and latest != have:
        return "upgrade"
    if state == "pinned" and have != version:
        return "pin"
    return "none"


def _app_ensure(params: dict, config: AgentConfig) -> str:
    state = str(params.get("state") or "")
    if state not in _ENSURE_STATES:
        raise RuntimeError(f"state must be one of {', '.join(sorted(_ENSURE_STATES))}, "
                           f"got {state!r}")
    app = _app_param(params, "app")
    if state == "pinned":
        version = _app_param(params, "version")
    elif params.get("version") not in (None, ""):
        raise RuntimeError("only a pinned app takes a version")
    else:
        version = ""
    source = str(params.get("source") or "")
    if source and source not in _PRIMARY_SOURCES | _STORE_SOURCES | _WINDOWS_SOURCES:
        raise RuntimeError(f"unknown source {source!r}")

    before = _recollect()
    row = _ensure_row(before, app, source)
    version_before = str(row.get("version") or "") if row else ""
    action = _ensure_decision(state, row, version)
    if action == "none":
        return ActionOutput(
            f"{app} is already {state}" + (f" at {version_before}" if version_before else ""),
            {"changed": False, "action": "none", "version_before": version_before,
             "version_after": version_before})

    # The row's own source when there is one: a registry entry is uninstalled
    # through the registry, not through whatever manager the host runs.
    target = {"app": app}
    act_source = str(row.get("source") or "") if row else source
    if act_source:
        target["source"] = act_source
    if action == "install":
        _app_install(target, config)
    elif action == "upgrade":
        _app_upgrade(target, config)
    elif action == "uninstall":
        _app_uninstall(target, config)
    else:
        if sys.platform == "win32" and act_source in _MANAGER_SOURCES_WINDOWS | {""}:
            # winget and Chocolatey only hold a version; they do not fetch it.
            _app_install({**target, "version": version}, config)
        _app_pin({**target, "version": version}, config)

    after = _ensure_row(_recollect(), app, act_source)
    version_after = str(after.get("version") or "") if after else ""
    return ActionOutput(
        f"{action} {app}: {version_before or 'absent'} → {version_after or 'absent'}",
        {"changed": True, "action": action, "version_before": version_before,
         "version_after": version_after})
