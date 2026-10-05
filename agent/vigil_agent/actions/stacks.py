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


_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$")


def _compose_file(params: dict) -> str:
    """The compose file's name in the working directory — compose.yaml for a
    stack Vigil created, the stack's own name for one it adopted."""
    name = str(params.get("compose_file") or COMPOSE_NAME)
    if not _FILE.match(name):
        raise ValueError(f"compose_file {name!r} must be a plain .yaml / .yml file name")
    return name


def _compose(workdir: Path, project: str, *args: str, timeout: int = 600,
             compose_file: str = COMPOSE_NAME) -> str:
    return ex._run([*ex._compose_cmd(), "-p", project, "--project-directory", str(workdir),
                    "-f", str(workdir / compose_file), *args],
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
    compose_file = _compose_file(params)
    # Redeemed first: a deploy that cannot have its secrets must not start
    # half-configured containers.
    env_text = client.fetch_stack_env(config, ticket) if ticket else None

    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / compose_file).write_text(compose)
    if env_text is not None:
        _write_private(workdir / ".env", env_text)
    output = _compose(workdir, project, "up", "-d", "--remove-orphans", compose_file=compose_file)
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


def _hash_report(project: str, args: list[str]) -> dict:
    """Which services would compose recreate? ``config --hash '*'`` against
    the config-hash label on each running container. Matching = untouched."""
    from .containers import _COMPOSE_PROJECT_LABEL

    try:
        out = ex._run([*ex._compose_cmd(), *args, "config", "--hash", "*"], timeout=120,
                      extra_env=ex._compose_env())
    except RuntimeError as exc:
        return {"error": str(exc)[:300]}
    wanted = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            wanted[parts[0]] = parts[1]
    running = {}
    import json as _json
    for c in ex._engine().get("/containers/json", query={"all": "1", "filters": _json.dumps(
            {"label": [f"{_COMPOSE_PROJECT_LABEL}={project}"]})}) or []:
        labels = c.get("Labels") or {}
        running[labels.get("com.docker.compose.service", "")] = labels.get(
            "com.docker.compose.config-hash", "")
    return {"match": sorted(s for s, h in wanted.items() if running.get(s) == h),
            "recreate": sorted(s for s, h in wanted.items() if running.get(s) != h)}


def _stack_read(params: dict, config: AgentConfig) -> str:
    """Read an existing stack for adoption and post it to its ticket."""
    from .. import client
    from .containers import _stack_compose_args

    project = _project(params)
    ticket = str(params.get("adopt_ticket") or "")
    if not _TICKET.match(ticket):
        raise ValueError("adopt_ticket must be a ticket id")
    args = _stack_compose_args(project)
    files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
    if len(files) != 1:
        raise ValueError(f"{project} uses {len(files)} compose files — Vigil adopts a stack "
                         f"with exactly one (merge the override in first)")
    compose_path = Path(files[0])
    workdir = Path(args[args.index("--project-directory") + 1]) if "--project-directory" in args \
        else compose_path.parent
    env_path = workdir / ".env"
    payload = {
        "project": project,
        "compose_file": compose_path.name,
        "working_dir": str(workdir),
        "compose": compose_path.read_text(),
        "env": env_path.read_text() if env_path.exists() else "",
        "hashes": _hash_report(project, args),
    }
    client.post_stack_read(config, ticket, payload)
    report = payload["hashes"]
    return ActionOutput(
        f"read {compose_path} and {'.env' if payload['env'] else 'no .env'}; "
        f"{len(report.get('match', []))} service(s) match what runs, "
        f"{len(report.get('recreate', []))} would be recreated",
        {"project": project, "would_recreate": len(report.get("recreate", []))})
