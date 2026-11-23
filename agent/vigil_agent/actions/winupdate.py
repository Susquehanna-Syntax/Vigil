"""Windows Update actions: scan for pending updates, install the filtered set.

Moved out of executor.py. Both handlers are Windows-only and delegate the COM
layer to ``vigil_agent.windows_update``; the same filter params apply to a scan
and to an install, so what an operator sees is what the install acts on. An
install never reboots — it reports ``reboot_required`` and stops, because an
action that restarts a machine unasked is exactly what this split exists to
prevent. executor re-exports every name defined here.
"""

from __future__ import annotations

import json

from ..config import AgentConfig
from ..executor import ActionOutput


def _windows_update_scan(params: dict, _config: AgentConfig) -> str:
    """Return pending Windows updates as JSON, filtered by the optional
    ``classifications`` / ``include_kb`` / ``exclude_kb`` / ``severity_floor``
    params (the same filters the install action applies)."""
    from .. import windows_update

    backend = windows_update.detect()
    if backend is None:
        raise ValueError("windows_update_scan is only supported on Windows")
    updates = windows_update.filter_updates(
        backend.scan(),
        classifications=params.get("classifications"),
        include_kb=params.get("include_kb"),
        exclude_kb=params.get("exclude_kb"),
        severity_floor=params.get("severity_floor"),
    )
    return ActionOutput(json.dumps({
        "supported": True,
        "count": len(updates),
        "updates": updates,
    }), {"count": len(updates)})


def _windows_update_install(params: dict, _config: AgentConfig) -> str:
    """Install the pending Windows updates that survive the filter params.

    Never reboots: the result reports ``reboot_required`` and the agent stops
    there. ``reboot`` is a separate action; an install action that reboots a
    machine is the failure this milestone exists to prevent.
    """
    from .. import windows_update

    backend = windows_update.detect()
    if backend is None:
        raise ValueError("windows_update_install is only supported on Windows")
    updates = windows_update.filter_updates(
        backend.scan(),
        classifications=params.get("classifications"),
        include_kb=params.get("include_kb"),
        exclude_kb=params.get("exclude_kb"),
        severity_floor=params.get("severity_floor"),
    )
    # Install() on an empty collection throws a COM error that reads like a
    # real failure, so an empty filter result is reported, not installed.
    if not updates:
        return ActionOutput(json.dumps({
            "supported": True,
            "result_code": windows_update.RESULT_NOT_STARTED,
            "reboot_required": False,
            "installed": [],
            "failed": [],
            "detail": "no updates matched the filter; nothing installed",
        }), {"installed_count": 0, "failed_count": 0, "reboot_required": False})
    result = backend.install([u["update_id"] for u in updates])
    result["supported"] = True
    return ActionOutput(
        json.dumps(result),
        {"installed_count": len(result["installed"]),
         "failed_count": len(result["failed"]),
         "reboot_required": bool(result["reboot_required"])})
