"""Account actions: create / delete a user, group membership, cron jobs.

Moved out of executor.py. Local accounts go to ``useradd`` / ``userdel`` /
``usermod`` through ``executor._run`` (no shell), with every name re-validated
against the safe username and group patterns here even though the server
already checked them. A crontab is read and rewritten in one argv round-trip;
command substitution in a job's command line is refused outright. executor
re-exports every name defined here.
"""

from __future__ import annotations

import subprocess

from .. import executor as ex
from ..config import AgentConfig
from ..executor import _SAFE_CRON_SCHEDULE, _SAFE_GROUP, _SAFE_USERNAME, ActionOutput
from ..procenv import clean_env


def _create_user(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")

    cmd = ["useradd"]

    groups = params.get("groups", "")
    if groups:
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.split(",") if g.strip()]
        for g in groups:
            if not _SAFE_GROUP.match(g):
                raise ValueError(f"Invalid group name: {g!r}")
        cmd.extend(["-G", ",".join(groups)])

    shell = params.get("shell", "")
    if shell:
        shell_path = ex._validate_path(shell, "shell")
        if not shell_path.exists():
            raise ValueError(f"Shell not found: {shell}")
        cmd.extend(["-s", str(shell_path)])

    cmd.append(username)
    return ActionOutput(ex._run(cmd), {"username": username})


def _delete_user(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")
    cmd = ["userdel"]
    if params.get("remove_home", False):
        cmd.append("--remove")
    cmd.append(username)
    return ActionOutput(ex._run(cmd), {"username": username})


def _add_user_to_group(params: dict, _config: AgentConfig) -> str:
    username = params.get("username", "")
    group = params.get("group", "")
    if not _SAFE_USERNAME.match(username):
        raise ValueError(f"Invalid username: {username!r}")
    if not _SAFE_GROUP.match(group):
        raise ValueError(f"Invalid group name: {group!r}")
    return ActionOutput(
        ex._run(["usermod", "-aG", group, username]),
        {"username": username, "group": group})


def _create_cron_job(params: dict, _config: AgentConfig) -> str:
    schedule = params.get("schedule", "")
    if not _SAFE_CRON_SCHEDULE.match(schedule):
        raise ValueError(f"Invalid cron schedule: {schedule!r}")

    command = params.get("command", "")
    if not command:
        raise ValueError("command is required")
    if any(c in command for c in ("`", "$(")):
        raise ValueError("Command contains dangerous shell substitution")

    user = params.get("user", "root")
    if not _SAFE_USERNAME.match(user):
        raise ValueError(f"Invalid user: {user!r}")

    cron_line = f"{schedule} {command}"

    # Read existing crontab, append new entry
    try:
        existing = subprocess.run(
            ["crontab", "-u", user, "-l"],
            capture_output=True, text=True, timeout=10, shell=False,
            env=clean_env(),
        )
        current = existing.stdout if existing.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        current = ""

    new_crontab = current.rstrip("\n") + "\n" + cron_line + "\n"

    proc = subprocess.run(
        ["crontab", "-u", user, "-"],
        input=new_crontab, capture_output=True, text=True,
        timeout=10, shell=False,
        env=clean_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to set crontab: {proc.stderr.strip()}")

    return ActionOutput(
        f"Added cron job for user {user}: {cron_line}", {"user": user})


def _delete_cron_job(params: dict, _config: AgentConfig) -> str:
    pattern = params.get("pattern", "")
    if not pattern:
        raise ValueError("pattern is required")

    user = params.get("user", "root")
    if not _SAFE_USERNAME.match(user):
        raise ValueError(f"Invalid user: {user!r}")

    result = subprocess.run(
        ["crontab", "-u", user, "-l"],
        capture_output=True, text=True, timeout=10, shell=False,
        env=clean_env(),
    )
    if result.returncode != 0:
        return ActionOutput(f"No crontab for user {user}",
                            {"user": user, "removed": 0})

    lines = result.stdout.splitlines()
    filtered = [line for line in lines if pattern not in line]
    removed = len(lines) - len(filtered)

    if removed == 0:
        return ActionOutput(f"No cron entries matched pattern {pattern!r}",
                            {"user": user, "removed": 0})

    new_crontab = "\n".join(filtered) + "\n"
    proc = subprocess.run(
        ["crontab", "-u", user, "-"],
        input=new_crontab, capture_output=True, text=True,
        timeout=10, shell=False,
        env=clean_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to update crontab: {proc.stderr.strip()}"
        )

    return ActionOutput(
        f"Removed {removed} cron entry/entries matching {pattern!r}",
        {"user": user, "removed": removed})
