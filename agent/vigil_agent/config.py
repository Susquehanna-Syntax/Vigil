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

_ALL_ACTIONS = {
    "restart_service",
    "restart_container",
    "stop_container",
    "start_container",
    "clear_temp_files",
    "clear_docker_logs",
    "run_package_updates",
    "execute_script",
    "reboot",
}

DEFAULT_CONFIG_PATHS = [
    Path("/etc/vigil/agent.yml"),
    Path("agent.yml"),
]


@dataclass
class AgentConfig:
    server_url: str
    agent_token: str
    mode: str = "managed"
    checkin_interval: int = 60
    data_dir: Path = field(default_factory=lambda: Path("/var/lib/vigil-agent"))
    allowlist: set[str] = field(default_factory=set)
    scripts_dir: Path = field(default_factory=lambda: Path("/etc/vigil/scripts"))
    config_path: Path | None = None

    def __post_init__(self):
        if self.mode not in _VALID_MODES:
            raise ValueError(f"Invalid mode '{self.mode}', must be one of: {_VALID_MODES}")
        if self.checkin_interval < 10:
            raise ValueError("checkin_interval must be at least 10 seconds")
        unknown = self.allowlist - _ALL_ACTIONS
        if unknown:
            raise ValueError(f"Unknown actions in allowlist: {unknown}")

    def task_allowed(self, action: str) -> bool:
        if self.mode == "monitor":
            return False
        if self.mode == "full_control":
            return True
        return action in self.allowlist


def _warn_permissions(path: Path) -> None:
    """Warn if the config file is readable by group/others (token exposure risk)."""
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
    """Load and validate agent configuration from YAML."""
    if path is None:
        for candidate in DEFAULT_CONFIG_PATHS:
            if candidate.exists():
                path = candidate
                break
    if path is None or not path.exists():
        raise FileNotFoundError(
            f"No config file found. Tried: {[str(p) for p in DEFAULT_CONFIG_PATHS]}"
        )

    _warn_permissions(path)

    with open(path) as f:
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
    allowlist_raw = raw.get("allowlist", [])
    allowlist = set(allowlist_raw) if isinstance(allowlist_raw, list) else set()

    data_dir = Path(raw.get("data_dir", "/var/lib/vigil-agent"))

    config = AgentConfig(
        server_url=server_url,
        agent_token=agent_token,
        mode=mode,
        checkin_interval=int(raw.get("checkin_interval", 60)),
        data_dir=data_dir,
        allowlist=allowlist,
        scripts_dir=Path(raw.get("scripts_dir", "/etc/vigil/scripts")),
        config_path=path,
    )

    # Persist auto-generated token back to config file
    if token_generated:
        raw["agent_token"] = agent_token
        with open(path, "w") as f:
            yaml.safe_dump(raw, f, default_flow_style=False)
        logger.info("Saved generated token to %s", path)

    return config
