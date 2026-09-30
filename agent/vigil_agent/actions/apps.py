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


def _app_upgrade(params: dict, _config: AgentConfig) -> str:
    app = _app_param(params, "app") if params.get("app") else ""
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


def _app_uninstall(params: dict, _config: AgentConfig) -> str:
    app = _app_param(params, "app")
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
