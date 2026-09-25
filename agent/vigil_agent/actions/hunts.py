"""Hunt actions: read-only discovery probes (M5).

Moved out of executor.py. The probes themselves live in ``vigil_agent.hunt``;
these handlers are one-liners that hand the probe to ``run_hunt``, which owns
the result shape, the result/timeout caps and the priority lowering.
"""

from __future__ import annotations

from .. import hunt
from ..config import AgentConfig


def _hunt_file(params: dict, _config: AgentConfig) -> str:
    """Find files by name/glob and optional sha256, size and age.

    Walks the targeted scope by default (every filesystem root with
    ``scope: full``, or the given ``paths``) and adds one ``file`` match per hit. Read-only: it never modifies,
    moves or deletes anything it finds.
    """
    return hunt.run_hunt(hunt.hunt_file, params)


def _hunt_package(params: dict, _config: AgentConfig) -> str:
    """Find installed packages by name/glob and version bounds.

    Versions compare with the package system's own rules (see
    ``vigil_agent.versions``). Read-only: one listing call, nothing installed
    or removed.
    """
    return hunt.run_hunt(hunt.hunt_package, params)


def _hunt_process(params: dict, _config: AgentConfig) -> str:
    """List running processes matching a name glob, cmdline substring or user.

    Read-only: a single process table walk, nothing started or killed.
    """
    return hunt.run_hunt(hunt.hunt_process, params)


def _hunt_port(params: dict, _config: AgentConfig) -> str:
    """List local socket bindings matching a port (or range), protocol and process.

    Read-only: one snapshot of the socket table.
    """
    return hunt.run_hunt(hunt.hunt_port, params)


def _hunt_service(params: dict, _config: AgentConfig) -> str:
    """List services matching a name glob and optional state / start mode.

    Read-only: systemctl or the SCM listing, nothing started or stopped.
    """
    return hunt.run_hunt(hunt.hunt_service, params)
