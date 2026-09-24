"""Agent configuration loading and validation."""

import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("vigil.config")

_VALID_MODES = {"monitor", "managed", "full_control"}

#: Actions that destroy the machine. Deliberately outside the mode system:
#: full_control means "any action the server asks for", and wiping the disk
#: must never be inside that promise. A compromised server cannot rebuild a
#: host whose local config did not opt in. See docs/reprovisioning.md §4.1.
#:
#: reprovision_preflight is absent on purpose — it is read-only, so it stays
#: an ordinary allowlistable action and an operator can survey rebuild
#: readiness across a fleet without arming anything.
REPROVISION_ACTIONS = frozenset({
    "reprovision_stage",
    "reprovision_commit",
    "reprovision_cleanup",
})

#: Real actions that are never allowlistable, and why they are refused.
#: run_command is arbitrary command execution — full_control only, enforced
#: again in the executor as defence in depth. The three destructive
#: reprovision actions are granted by allow_reprovision instead.
_NEVER_ALLOWLISTABLE = {
    "run_command",
    "reprovision_stage",
    "reprovision_commit",
    "reprovision_cleanup",
}

_ALL_ACTIONS = {
    # Service management
    "restart_service", "start_service", "stop_service", "reload_service",
    "enable_service", "disable_service", "check_service",
    # Container management
    "restart_container", "stop_container", "start_container",
    "pull_image", "recreate_container", "remove_container",
    "update_container", "docker_compose_up", "docker_compose_down",
    "clear_docker_logs", "check_docker_updates",
    # File / directory operations
    "write_file", "create_directory", "delete_path",
    "copy_file", "move_file", "set_permissions",
    # Package management
    "install_package", "remove_package", "update_package",
    "run_package_updates",
    # System
    "clear_temp_files", "execute_script", "reboot", "set_hostname",
    # run_command intentionally excluded — full_control only, enforced in
    # the executor handler itself as defense-in-depth.
    # Networking
    "add_firewall_rule", "remove_firewall_rule", "list_firewall_rules",
    "set_firewall_policy", "enable_firewall", "disable_firewall",
    # Windows Update
    "windows_update_scan", "windows_update_install",
    # User management
    "create_user", "delete_user", "add_user_to_group",
    # Cron
    "create_cron_job", "delete_cron_job",
    # Host tagging — server-side metadata, so these do no work here. The
    # agent reports the step and the server applies the tags it already
    # signed into the task. Allowlistable because nothing on this machine
    # changes.
    "add_tag", "remove_tag",
    # Vulnerability scanning
    "request_nessus_scan", "request_network_scan",
    "run_trivy_scan", "trivy_db_update",
    # Agent lifecycle — allowlisting this lets a managed-mode agent
    # accept signed self-update tasks (the binary digest rides inside
    # the Ed25519-signed payload).
    "update_agent",
    # Reprovisioning readiness probe — read-only, so it belongs in an
    # allowlist like any other low-risk action. The three destructive
    # reprovision actions are deliberately NOT here: they are not
    # allowlistable at all, only allow_reprovision grants them, so naming
    # one in an allowlist draws the "ignoring unknown action" warning
    # rather than silently appearing to work.
    "reprovision_preflight",
}

def _default_scripts_dir(is_windows: bool | None = None) -> Path:
    """Where execute_script looks for scripts, per platform.

    "/etc/vigil/scripts" on Windows resolves to a path on the current drive
    that nothing creates — the same shape of bug as the config search path.

    *is_windows* is a parameter for the same reason as _default_config_paths:
    patching os.name makes pathlib build a WindowsPath on Linux and raise.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if is_windows:
        program_data = os.environ.get("ProgramData")
        if program_data:
            return Path(program_data) / "Vigil" / "scripts"
    return Path("/etc/vigil/scripts")


def _default_config_paths(is_windows: bool | None = None) -> list[Path]:
    """Where to look for agent.yml when no -c and no VIGIL_CONFIG_PATH.

    *is_windows* is a parameter rather than a read of os.name so tests can
    exercise both branches: patching os.name globally makes pathlib try to
    build a WindowsPath on Linux and raise NotImplementedError.

    Windows needs its own entry. "/etc/vigil/agent.yml" resolves there to
    "\\etc\\vigil\\agent.yml" on the current drive, which nothing writes,
    so the service — whose binPath carries no -c — started, found no config
    and exited:

        FileNotFoundError: No config file found. Tried: VIGIL_CONFIG_PATH,
        ['\\etc\\vigil\\agent.yml', 'agent.yml']

    install.ps1 has always written C:\\ProgramData\\Vigil\\agent.yml. The
    agent simply never looked there, so the Windows service could not have
    worked regardless of how it was packaged.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    paths: list[Path] = []
    if is_windows:
        program_data = os.environ.get("ProgramData")
        if program_data:
            paths.append(Path(program_data) / "Vigil" / "agent.yml")
    else:
        paths.append(Path("/etc/vigil/agent.yml"))
    paths.append(Path("agent.yml"))
    return paths


#: Evaluated at import for callers that read it directly; load_config() calls
#: _default_config_paths() so a test can patch the environment.
DEFAULT_CONFIG_PATHS = _default_config_paths()


@dataclass
class AgentConfig:
    server_url: str
    agent_token: str
    mode: str = "managed"
    # Permit this machine to be remotely wiped and reinstalled. NOT implied
    # by full_control — see REPROVISION_ACTIONS above.
    allow_reprovision: bool = False
    checkin_interval: int = 60
    # Seconds between Docker Hub image-update checks. The check uses
    # unmetered HEAD requests, but the floor keeps misconfigured agents
    # from hammering the registry's auth endpoint.
    docker_check_interval: int = 21600
    data_dir: Path = field(default_factory=lambda: Path("/var/lib/vigil-agent"))
    allowlist: set[str] = field(default_factory=set)
    scripts_dir: Path = field(default_factory=lambda: _default_scripts_dir())
    # Free-form tags advertised to the server at every checkin. Server-side
    # tags take precedence: this list is used to seed/augment, never to
    # overwrite tags an operator has set in the console.
    tags: list[str] = field(default_factory=list)
    # Process names sampled at every scrape whether or not they rank in the
    # top ten. Without this a quiet process is invisible: the collector only
    # ships the busiest processes, so a chart of one named service would be
    # full of holes exactly when the service is behaving.
    process_watch: list[str] = field(default_factory=list)
    # GPU telemetry beyond the core four (utilisation, memory, temperature,
    # power). Off by default: the wide set is roughly three times the points
    # per GPU per scrape, which is a real cost on a multi-GPU host.
    gpu_extended: bool = False
    config_path: Path | None = None

    def __post_init__(self):
        if self.mode not in _VALID_MODES:
            raise ValueError(f"Invalid mode '{self.mode}', must be one of: {_VALID_MODES}")
        if self.checkin_interval < 10:
            raise ValueError("checkin_interval must be at least 10 seconds")
        if self.docker_check_interval < 300:
            raise ValueError("docker_check_interval must be at least 300 seconds")
        # Unknown allowlist entries are dropped with a warning, not fatal:
        # an agent.yml written for a newer agent (or with a typo) must never
        # crash-loop the agent and take monitoring down with it. The dropped
        # action simply stays un-allowlisted — tasks naming it are rejected
        # with a clear reason, and it starts working after the agent updates.
        unknown = self.allowlist - _ALL_ACTIONS
        # Separate "not a real action" from "real, but deliberately not
        # allowlistable". Both are ignored, but only one is a typo, and
        # telling an operator to look for a typo in `run_command` sends them
        # hunting for something that is not there.
        not_allowlistable = unknown & _NEVER_ALLOWLISTABLE
        unknown -= not_allowlistable
        # Drop both, always. Splitting the warning must not split the
        # enforcement: leaving run_command in the allowlist would let a
        # managed-mode agent accept arbitrary command execution, which is the
        # single thing this exclusion exists to prevent.
        self.allowlist = self.allowlist - not_allowlistable - unknown
        if not_allowlistable:
            logger.warning(
                "Ignoring allowlist entries that cannot be allowlisted: %s. "
                "These are real actions, deliberately excluded because they "
                "grant too much: run_command executes arbitrary commands and "
                "needs mode: full_control; the destructive reprovision "
                "actions need allow_reprovision: true.",
                sorted(not_allowlistable))
        if unknown:
            logger.warning(
                "Ignoring unknown allowlist actions (typo, or this agent "
                "binary is older than the config): %s", sorted(unknown),
            )
        # Normalize tags: strip whitespace, drop blanks, dedupe, lowercase.
        cleaned: list[str] = []
        seen: set[str] = set()
        for tag in self.tags or []:
            if not isinstance(tag, str):
                continue
            t = tag.strip().lower()
            if not t or t in seen:
                continue
            if len(t) > 40:
                raise ValueError(f"tag {tag!r} too long (max 40 chars)")
            seen.add(t)
            cleaned.append(t)
        self.tags = cleaned
        # Same normalisation as tags, minus the lowercasing: process names are
        # case-sensitive on every platform the agent runs on.
        watched: list[str] = []
        seen_procs: set[str] = set()
        for name in self.process_watch or []:
            if not isinstance(name, str):
                continue
            n = name.strip()
            if not n or n in seen_procs:
                continue
            if len(n) > 120:
                raise ValueError(f"process_watch entry {name!r} too long (max 120 chars)")
            seen_procs.add(n)
            watched.append(n)
        self.process_watch = watched

    def task_allowed(self, action: str) -> bool:
        if action in REPROVISION_ACTIONS:
            # Checked before mode so no later branch can grant it: not
            # full_control, not the allowlist — this flag and nothing else.
            # Monitor mode still executes nothing at all.
            return self.allow_reprovision and self.mode != "monitor"
        if self.mode == "monitor":
            return False
        if self.mode == "full_control":
            return True
        return action in self.allowlist


def _warn_permissions(path: Path) -> None:
    """Warn if the config file is readable by group/others (token exposure risk)."""
    if os.name != "posix":
        # Windows has no POSIX mode bits. Python synthesises st_mode there, so
        # this check fired on every start regardless of the real ACL — and told
        # the operator to run `chmod 600 C:\ProgramData\Vigil\agent.yml`,
        # which is not a command they have. install.ps1 restricts the file with
        # icacls instead; a real ACL check here would need pywin32 and is not
        # worth a hard dependency for a warning.
        return
    try:
        st = path.stat()
        if st.st_mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning(
                "Config file %s is readable by group/others. "
                "Run: chmod 600 %s",
                path,
                path,
            )
    except OSError:
        pass


def load_config(path: Path | None = None) -> AgentConfig:
    """Load and validate agent configuration from YAML.

    Resolution order: explicit ``-c`` path, then the ``VIGIL_CONFIG_PATH``
    environment variable (what the Dockerfile and config.example.yml
    document), then DEFAULT_CONFIG_PATHS.
    """
    if path is None:
        env_path = os.environ.get("VIGIL_CONFIG_PATH", "").strip()
        if env_path:
            path = Path(env_path)
            if not path.exists():
                raise FileNotFoundError(
                    f"VIGIL_CONFIG_PATH points at {env_path}, which does not exist"
                )
    if path is None:
        for candidate in _default_config_paths():
            if candidate.exists():
                path = candidate
                break
    if path is None or not path.exists():
        raise FileNotFoundError(
            f"No config file found. Tried: VIGIL_CONFIG_PATH, "
            f"{[str(p) for p in _default_config_paths()]}"
        )

    _warn_permissions(path)

    # utf-8-sig, not utf-8: it strips a UTF-8 BOM if present and behaves
    # identically when absent. PowerShell 5.1's `Set-Content -Encoding UTF8`
    # writes one, and so does Notepad — which is what a Windows admin edits
    # this file with. With a BOM the first key parses as "\ufeffserver_url"
    # and the agent dies with "server_url is required" while the operator is
    # looking straight at a config that has it.
    with open(path, encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}

    server_url = raw.get("server_url", "").rstrip("/")
    if not server_url:
        raise ValueError("server_url is required in config")

    agent_token = raw.get("agent_token", "").strip()
    token_generated = False
    if not agent_token:
        agent_token = secrets.token_urlsafe(32)
        token_generated = True
        logger.info("Generated new agent token")

    mode = raw.get("mode", "managed")
    allow_reprovision = bool(raw.get("allow_reprovision", False))
    allowlist_raw = raw.get("allowlist", [])
    allowlist = set(allowlist_raw) if isinstance(allowlist_raw, list) else set()

    data_dir = Path(raw.get("data_dir", "/var/lib/vigil-agent"))

    raw_tags = raw.get("tags") or []
    if not isinstance(raw_tags, list):
        raise ValueError("tags must be a list of strings")

    raw_watch = raw.get("process_watch") or []
    if not isinstance(raw_watch, list):
        raise ValueError("process_watch must be a list of process names")

    config = AgentConfig(
        server_url=server_url,
        agent_token=agent_token,
        mode=mode,
        allow_reprovision=allow_reprovision,
        checkin_interval=int(raw.get("checkin_interval", 60)),
        docker_check_interval=int(raw.get("docker_check_interval", 21600)),
        data_dir=data_dir,
        allowlist=allowlist,
        scripts_dir=Path(raw["scripts_dir"]) if raw.get("scripts_dir")
        else _default_scripts_dir(),
        tags=raw_tags,
        process_watch=raw_watch,
        gpu_extended=bool(raw.get("gpu_extended", False)),
        config_path=path,
    )

    # Persist auto-generated token back to config file
    if token_generated:
        raw["agent_token"] = agent_token
        # 0600 before anything is written into it. This file holds the bearer
        # credential that authenticates this machine to the server, and the
        # write-back used to inherit the umask — 0644 under the default, so a
        # fresh install left a world-readable token on every monitored box and
        # only logged a warning about it. The pinned server key and the nonce
        # store next to it were both already chmod'd; this one was missed.
        #
        # os.open with the mode set at creation, rather than open() then
        # chmod(), so there is no window where the file exists with the token
        # in it and the wrong permissions on it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(raw, f, default_flow_style=False)
        except Exception:
            os.close(fd) if not os.path.exists(path) else None
            raise
        # An existing file keeps its old mode through O_CREAT, so tighten it
        # too — an install upgraded from a version that wrote 0644 should not
        # stay 0644 forever.
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("Could not set 0600 on %s — check it by hand", path)
        logger.info("Saved generated token to %s (mode 0600)", path)

    return config
