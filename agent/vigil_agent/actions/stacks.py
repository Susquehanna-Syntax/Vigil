"""stack_deploy / stack_remove — Vigil-managed compose stacks (M11).

The compose file arrives in the signed task; the .env does not. A deploy
carries a one-time ticket the agent redeems over its own connection just
before writing the file, which is created mode 0600 and never logged.
Compose runs with ``-p <project>`` against the agent's engine socket.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .. import collector
from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput

STACKS_ROOT = Path("/opt/vigil/stacks")
COMPOSE_NAME = "compose.yaml"
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_TICKET = re.compile(r"^[0-9a-f-]{36}$")


def _project(params: dict) -> str:
    project = str(params.get("project") or "")
    if not _PROJECT.match(project):
        raise ValueError(f"{project!r} is not a compose project name")
    return project


def _workdir(params: dict) -> Path:
    raw = str(params.get("working_dir") or "")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"working_dir {raw!r} must be an absolute path without '..'")
    return path


def _write_private(path: Path, text: str) -> None:
    """Create or replace *path* readable by its owner only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _compose(workdir: Path, project: str, *args: str, timeout: int = 600) -> str:
    return ex._run([*ex._compose_cmd(), "-p", project, "--project-directory", str(workdir),
                    "-f", str(workdir / COMPOSE_NAME), *args],
                   timeout=timeout, extra_env=ex._compose_env())


def _stack_deploy(params: dict, config: AgentConfig) -> str:
    from .. import client

    project = _project(params)
    workdir = _workdir(params)
    compose = str(params.get("compose") or "")
    if not compose.strip():
        raise ValueError("compose is empty")
    ticket = str(params.get("env_ticket") or "")
    if ticket and not _TICKET.match(ticket):
        raise ValueError("env_ticket must be a ticket id")
    # Redeemed first: a deploy that cannot have its secrets must not start
    # half-configured containers.
    env_text = client.fetch_stack_env(config, ticket) if ticket else None

    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / COMPOSE_NAME).write_text(compose)
    if env_text is not None:
        _write_private(workdir / ".env", env_text)
    output = _compose(workdir, project, "up", "-d", "--remove-orphans")
    collector.request_docker_recheck()
    revision = params.get("revision")
    return ActionOutput(output or f"deployed {project}",
                        {"project": project,
                         "revision": revision if isinstance(revision, int) else 0})


def _stack_remove(params: dict, _config: AgentConfig) -> str:
    project = _project(params)
    workdir = _workdir(params)
    delete = params.get("delete_files")
    if delete is not None and not isinstance(delete, bool):
        raise ValueError("delete_files must be true or false")
    # Only ever a folder Vigil itself created — refused before anything runs.
    if delete and (workdir.parent != STACKS_ROOT or workdir.name != project):
        raise ValueError(f"refusing to delete {workdir}: only {STACKS_ROOT}/<project> is Vigil's")
    output = _compose(workdir, project, "down", timeout=300) if (workdir / COMPOSE_NAME).exists() \
        else f"{project}: no compose file at {workdir} — nothing to take down"
    deleted = False
    if delete:
        if workdir.exists():
            shutil.rmtree(workdir)
            deleted = True
    collector.request_docker_recheck()
    return ActionOutput(output, {"project": project, "files_deleted": deleted})
