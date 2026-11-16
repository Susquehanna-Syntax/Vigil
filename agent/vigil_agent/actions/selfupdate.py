"""The agent's own actions: self-update, and the reprovisioning steps.

Moved out of executor.py. ``update_agent`` downloads the platform's artifact
from the server and only swaps it in after a SHA-256 the server placed inside
the Ed25519-signed task verifies; a Windows zip (a PyInstaller onedir build,
which a running executable cannot replace in place) goes through a detached
helper that does the directory swap once the service has stopped. Before the
restart it mirrors ``/etc/environment``'s proxy into the unit so the new
binary comes back with working egress. The reprovisioning handlers are thin
forwards to ``vigil_agent.reprovision``; they are gated by
``AgentConfig.task_allowed``, never by mode or the allowlist. executor
re-exports every name defined here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .. import update_guard
from ..config import AgentConfig
from ..executor import ActionOutput
from ..procenv import clean_env

logger = logging.getLogger("vigil.executor")


def _sha256_file(path) -> str:
    """Return the lowercase hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_systemd_proxy(config: AgentConfig) -> str:
    """Mirror the host's ``/etc/environment`` proxy into the agent's unit.

    systemd services don't inherit a login shell's environment, so on a
    proxied network the agent — and any task that shells out to
    curl/wget, like installing Trivy — can't reach the internet even
    though the host can. On every self-update we read ``/etc/environment``
    (the standard system-wide env file) and, if it defines an HTTP(S)
    proxy, drop it into a ``vigil-agent.service.d`` override so the
    post-update restart comes up with working egress. Loopback and the
    Vigil server stay direct.

    Best-effort and Linux/systemd only — any failure is logged and
    ignored so it can never break the binary swap that already happened.
    """
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return "skipped (not linux/systemd)"
    env_file = Path("/etc/environment")
    if not env_file.exists():
        return "no /etc/environment"
    try:
        vals: dict[str, str] = {}
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.lower().startswith("export "):
                key = key[len("export "):].strip()
            key = key.lower()
            val = val.strip().strip('"').strip("'")
            if key in ("http_proxy", "https_proxy", "no_proxy") and val:
                vals[key] = val

        http_p = vals.get("http_proxy", "")
        https_p = vals.get("https_proxy", "")
        if not http_p and not https_p:
            return "no proxy in /etc/environment"

        # Keep loopback + the Vigil server direct so internal check-ins
        # never detour through the proxy.
        server_host = re.sub(r"^https?://", "", config.server_url or "").split("/")[0].split(":")[0]
        no_proxy_parts = ["localhost", "127.0.0.1"]
        if server_host:
            no_proxy_parts.append(server_host)
        if vals.get("no_proxy"):
            no_proxy_parts.append(vals["no_proxy"])
        no_proxy = ",".join(no_proxy_parts)

        lines = ["[Service]"]
        for name, value in (("HTTP_PROXY", http_p), ("HTTPS_PROXY", https_p)):
            if value:
                lines.append(f"Environment={name}={value}")
                lines.append(f"Environment={name.lower()}={value}")
        lines.append(f"Environment=NO_PROXY={no_proxy}")
        lines.append(f"Environment=no_proxy={no_proxy}")
        content = "\n".join(lines) + "\n"

        drop_dir = Path("/etc/systemd/system/vigil-agent.service.d")
        drop_dir.mkdir(parents=True, exist_ok=True)
        (drop_dir / "10-vigil-proxy.conf").write_text(content)
        subprocess.run(["systemctl", "daemon-reload"], timeout=10,
                       capture_output=True, env=clean_env())
        return "applied proxy drop-in from /etc/environment"
    except Exception as exc:
        logger.warning("Proxy drop-in sync failed: %s", exc)
        return f"failed: {exc}"


def _reprovision_preflight(params: dict, config: AgentConfig) -> str:
    from .. import reprovision

    return reprovision.preflight(params, config)


def _reprovision_stage(params: dict, config: AgentConfig) -> str:
    from .. import reprovision

    return reprovision.stage(params, config)


def _reprovision_commit(params: dict, config: AgentConfig) -> str:
    from .. import reprovision

    return reprovision.commit(params, config)


def _reprovision_cleanup(params: dict, config: AgentConfig) -> str:
    from .. import reprovision

    return reprovision.cleanup(params, config)


def _looks_like_zip(path: str) -> bool:
    """True when the downloaded artifact is a zip rather than a bare binary."""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"PK"
    except OSError:
        return False


def _stage_onedir_update(archive_path: str, current_exe: Path, *, config=None,
                         to_version: str = "unknown") -> None:
    """Unpack a onedir update beside the install and swap it in on restart.

    Windows cannot replace a running executable — the file is locked for as
    long as the process lives, and a onedir build is a whole directory of DLLs
    besides. So the new build is extracted next to the old one and a detached
    helper does the swap once the service has actually stopped.

    Refuses rather than improvises if the layout is not what it expects. A
    half-swapped agent directory is worse than a failed update: the update can
    be retried, a broken install needs someone at the machine.
    """
    import zipfile

    install_dir = current_exe.parent
    if sys.platform != "win32":
        raise ValueError(
            "received a zip agent artifact on a non-Windows platform; "
            "refusing to self-update")

    staging = install_dir.parent / (install_dir.name + ".new")
    backup = install_dir.parent / (install_dir.name + ".old")
    for path in (staging, backup):
        shutil.rmtree(path, ignore_errors=True)

    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(staging)
    os.unlink(archive_path)

    staged = list(staging.rglob(current_exe.name))
    if not staged:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(
            f"the update archive contains no {current_exe.name}; "
            "refusing to swap it in")
    if config is not None:
        # The staged build proves itself where it lies, before the service is
        # touched. A running service cannot put its own folder back, so on
        # Windows this check is the whole protection.
        try:
            to_version = update_guard.run_preflight(staged[0], config) or to_version
        except ValueError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        update_guard.begin(config.data_dir, to_version=to_version,
                           exe=current_exe, previous=None)

    # A detached cmd: this process is about to be stopped, so the swap cannot
    # run inside it. Waits for the service to stop before touching anything.
    script = install_dir.parent / "vigil-agent-update.cmd"
    script.write_text(
        "@echo off\r\n"
        "sc stop vigil-agent >nul 2>&1\r\n"
        "for /l %%i in (1,1,30) do (\r\n"
        '  sc query vigil-agent | find "STOPPED" >nul && goto swap\r\n'
        "  timeout /t 1 /nobreak >nul\r\n"
        ")\r\n"
        ":swap\r\n"
        f'rmdir /s /q "{backup}" >nul 2>&1\r\n'
        f'move "{install_dir}" "{backup}" >nul 2>&1\r\n'
        f'move "{staging}" "{install_dir}" >nul 2>&1\r\n'
        f'if not exist "{install_dir}\\{current_exe.name}" '
        f'move "{backup}" "{install_dir}" >nul 2>&1\r\n'
        "sc start vigil-agent >nul 2>&1\r\n"
        f'rmdir /s /q "{backup}" >nul 2>&1\r\n'
        # Delete the helper itself. "start /b cmd /c del" detaches so the
        # script is not holding its own file open when the delete lands;
        # without it every update leaves a .cmd in Program Files.
        f'start /b "" cmd /c del /q "{script}"\r\n',
        encoding="ascii")
    subprocess.Popen(
        ["cmd", "/c", "start", "/b", "", str(script)],
        env=clean_env(), close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )


def _update_agent(params: dict, config: AgentConfig) -> str:
    """Download the latest agent binary from the server and replace this binary.

    The agent restarts itself via systemctl 3 seconds after the binary is
    replaced, giving this task result time to be reported first.
    """
    import requests as _requests

    platform = (params.get("platform") or "").strip()
    if not platform:
        if sys.platform == "win32":
            platform = "windows-amd64"
        elif sys.platform == "darwin":
            # Apple Silicon only since 2026.15: no Intel build is published.
            platform = "darwin-arm64" if os.uname().machine == "arm64" else "darwin-amd64"
        elif sys.platform.startswith("freebsd"):
            machine = os.uname().machine
            if machine not in ("amd64", "x86_64"):
                raise RuntimeError(f"No Vigil agent build for FreeBSD on {machine}")
            platform = "freebsd-amd64"
        else:
            machine = os.uname().machine
            platform = "linux-arm64" if machine in ("aarch64", "arm64") else "linux-amd64"
    if platform == "darwin-amd64":
        raise RuntimeError(
            "This Mac has an Intel processor. Vigil agents are built for Apple "
            "Silicon only (macOS no longer supports Intel Macs), so there is no "
            "update to install.")

    # The replacement binary is verified against a SHA-256 the server placed
    # inside this Ed25519-signed task. A TLS-only download is not a strong
    # enough proof for swapping the whole agent executable, so without a
    # verified digest we refuse to self-update.
    sha_map = params.get("binary_sha256")
    expected_sha = ""
    if isinstance(sha_map, dict):
        expected_sha = str(sha_map.get(platform) or "").strip().lower()
    if not expected_sha:
        raise ValueError(
            f"update_agent task carries no verified SHA-256 for platform "
            f"{platform!r}; refusing to self-update"
        )

    url = f"{config.server_url}/agent/download/{platform}/"
    token = config.agent_token

    current_exe = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).resolve()

    resp = _requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=(10, 120),
        stream=True,
    )
    resp.raise_for_status()

    tmp_fd, tmp_path = tempfile.mkstemp(dir=current_exe.parent, prefix=".vigil-agent-update-")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
        actual_sha = _sha256_file(tmp_path)
        if actual_sha != expected_sha:
            raise ValueError(
                f"Downloaded agent binary failed SHA-256 verification: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        new_version = resp.headers.get("X-Vigil-Version", "unknown")
        if _looks_like_zip(tmp_path):
            # Windows ships a PyInstaller --onedir build as a zip, because a
            # --onefile executable cannot host a Windows service. Writing that
            # archive over the service binary would replace the agent with a
            # zip file and brick the host, so the whole directory is swapped
            # instead, after the service has stopped and released its files.
            # The staged build proves itself before the helper is started.
            _stage_onedir_update(tmp_path, current_exe, config=config,
                                 to_version=new_version)
        else:
            os.chmod(tmp_path, 0o755)
            # The new build proves it runs here and reaches the server before
            # it replaces the agent that could have said it does not.
            new_version = update_guard.run_preflight(Path(tmp_path), config) or new_version
            previous = update_guard.keep_previous(current_exe)
            update_guard.begin(config.data_dir, to_version=new_version,
                               exe=current_exe, previous=previous)
            try:
                os.replace(tmp_path, current_exe)
            except OSError:
                update_guard.forget(config.data_dir)
                raise
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Self-heal proxy egress: reload the unit with any /etc/environment
    # proxy *before* the restart below picks up the new binary, so a
    # proxied host comes back online able to reach the internet.
    proxy_status = _sync_systemd_proxy(config)
    logger.info("update_agent proxy sync: %s", proxy_status)

    def _restart_after_delay():
        time.sleep(3)
        update_guard.restart_service()

    t = threading.Thread(target=_restart_after_delay, daemon=True)
    t.start()

    return ActionOutput(f"Agent updated to {new_version} ({platform}); restarting in 3 s",
                        {"version": new_version})
