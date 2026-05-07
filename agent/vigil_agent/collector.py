"""System metric collection using psutil.

Returns metric dicts ready for the checkin payload. Each metric is:
  {"category": str, "metric": str, "value": float, "labels": dict, "time": str}
"""

import http.client as _http_client
import json as _json
import logging
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

logger = logging.getLogger("vigil.collector")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _point(category: str, metric: str, value: float, labels: dict | None = None) -> dict:
    return {
        "category": category,
        "metric": metric,
        "value": round(value, 2),
        "labels": labels or {},
        "time": _now_iso(),
    }


def collect_cpu() -> list[dict]:
    points = []
    per_cpu = psutil.cpu_percent(interval=1, percpu=True)
    for i, pct in enumerate(per_cpu):
        points.append(_point("cpu", "usage_percent", pct, {"core": str(i)}))
    points.append(_point("cpu", "usage_percent", psutil.cpu_percent(), {"core": "total"}))

    load_1, load_5, load_15 = psutil.getloadavg()
    points.append(_point("cpu", "load_1m", load_1))
    points.append(_point("cpu", "load_5m", load_5))
    points.append(_point("cpu", "load_15m", load_15))
    return points


def collect_memory() -> list[dict]:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return [
        _point("memory", "total_bytes", mem.total),
        _point("memory", "used_bytes", mem.used),
        _point("memory", "available_bytes", mem.available),
        _point("memory", "usage_percent", mem.percent),
        _point("memory", "swap_total_bytes", swap.total),
        _point("memory", "swap_used_bytes", swap.used),
        _point("memory", "swap_usage_percent", swap.percent),
    ]


def collect_disk() -> list[dict]:
    points = []
    seen_devices = set()
    for part in psutil.disk_partitions(all=False):
        if part.device in seen_devices:
            continue
        seen_devices.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        labels = {"mount": part.mountpoint, "device": part.device, "fstype": part.fstype}
        points.append(_point("disk", "total_bytes", usage.total, labels))
        points.append(_point("disk", "used_bytes", usage.used, labels))
        points.append(_point("disk", "usage_percent", usage.percent, labels))
    return points


def collect_network() -> list[dict]:
    points = []
    counters = psutil.net_io_counters(pernic=True)
    for iface, stats in counters.items():
        if iface == "lo":
            continue
        labels = {"interface": iface}
        points.append(_point("network", "bytes_sent", stats.bytes_sent, labels))
        points.append(_point("network", "bytes_recv", stats.bytes_recv, labels))
        points.append(_point("network", "errors_in", stats.errin, labels))
        points.append(_point("network", "errors_out", stats.errout, labels))
        points.append(_point("network", "drops_in", stats.dropin, labels))
        points.append(_point("network", "drops_out", stats.dropout, labels))
    return points


def collect_top_processes(n: int = 10) -> list[dict]:
    """Collect the top N processes by CPU and memory usage."""
    points = []
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            if info["pid"] == 0:
                continue
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Top by CPU
    by_cpu = sorted(procs, key=lambda p: p["cpu_percent"] or 0, reverse=True)[:n]
    for rank, p in enumerate(by_cpu):
        labels = {"pid": str(p["pid"]), "name": p["name"] or "unknown", "rank": str(rank)}
        points.append(_point("process", "cpu_percent", p["cpu_percent"] or 0, labels))

    # Top by memory
    by_mem = sorted(procs, key=lambda p: p["memory_percent"] or 0, reverse=True)[:n]
    for rank, p in enumerate(by_mem):
        labels = {"pid": str(p["pid"]), "name": p["name"] or "unknown", "rank": str(rank)}
        points.append(_point("process", "memory_percent", p["memory_percent"] or 0, labels))

    return points


def collect_temperatures() -> list[dict]:
    points = []
    try:
        all_temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return points
    for sensor_name, entries in all_temps.items():
        for entry in entries:
            label = entry.label or sensor_name
            labels = {"sensor": sensor_name, "label": label}
            points.append(_point("temperature", "celsius", entry.current, labels))
            if entry.high is not None:
                points.append(_point("temperature", "high_celsius", entry.high, labels))
            if entry.critical is not None:
                points.append(_point("temperature", "critical_celsius", entry.critical, labels))
    return points


def collect_all() -> list[dict]:
    """Collect all available system metrics."""
    metrics = []
    collectors = [collect_cpu, collect_memory, collect_disk, collect_network,
                  collect_top_processes, collect_temperatures]
    for fn in collectors:
        try:
            metrics.extend(fn())
        except Exception:
            logger.exception("Collector %s failed", fn.__name__)
    return metrics


# ── Inventory collection ────────────────────────────────────────────────────
#
# Inventory is collected once and shipped alongside the regular metric
# payload. It moves at human timescales (hardware doesn't change often), so
# the runtime can choose to send it on a slower cadence than metrics.

_DMI_PATH = Path("/sys/class/dmi/id")
_DMI_FIELDS = {
    "service_tag": ["product_serial", "chassis_serial", "board_serial"],
    "manufacturer": ["sys_vendor", "board_vendor", "chassis_vendor"],
    "model": ["product_name", "board_name"],
}


def _read_dmi_field(filenames: list[str]) -> str:
    """Best-effort read of /sys/class/dmi/id/*. Returns "" on any failure."""
    for name in filenames:
        path = _DMI_PATH / name
        try:
            value = path.read_text(errors="replace").strip()
        except (OSError, PermissionError):
            continue
        if value and value.lower() not in {"to be filled by o.e.m.", "unknown", "default string", "system manufacturer", "system product name"}:
            return value
    return ""


def _read_mac_addresses() -> dict[str, str]:
    """Return {iface: mac} for every non-loopback interface psutil reports."""
    out: dict[str, str] = {}
    try:
        addrs = psutil.net_if_addrs()
    except Exception:
        return out
    for iface, entries in addrs.items():
        if iface == "lo":
            continue
        for entry in entries:
            # AF_LINK on macOS, AF_PACKET on Linux — both expose .address
            fam_name = getattr(entry.family, "name", str(entry.family))
            if "PACKET" in fam_name or "LINK" in fam_name:
                mac = (entry.address or "").lower()
                if re.fullmatch(r"[0-9a-f:]{17}", mac) and mac != "00:00:00:00:00:00":
                    out[iface] = mac
                    break
    return out


def _read_disks() -> list[dict]:
    """Return a list of {device, size_bytes} for fixed disks."""
    disks: list[dict] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:
        return disks
    seen_devices: set[str] = set()
    for part in partitions:
        device = part.device
        if device in seen_devices:
            continue
        seen_devices.add(device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": device,
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "size_bytes": int(usage.total),
            })
        except (PermissionError, OSError):
            continue
    return disks


def collect_inventory() -> dict:
    """Best-effort hardware inventory snapshot.

    Returns a dict with whatever information could be gathered. Fields that
    require root or privileged access (DMI reads can on some distros) fall
    back to "" / 0. The server stores this verbatim alongside the host.
    """
    inv: dict = {}
    try:
        mem = psutil.virtual_memory()
        inv["ram_total_bytes"] = int(mem.total)
    except Exception:
        inv["ram_total_bytes"] = 0

    try:
        inv["cpu_cores"] = int(psutil.cpu_count(logical=True) or 0)
    except Exception:
        inv["cpu_cores"] = 0

    cpu_model = ""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        try:
            for line in cpuinfo.read_text(errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    _, _, value = line.partition(":")
                    cpu_model = value.strip()
                    break
        except OSError:
            pass
    if not cpu_model:
        cpu_model = platform.processor() or platform.machine() or ""
    inv["cpu_model"] = cpu_model

    for key, candidates in _DMI_FIELDS.items():
        inv[key] = _read_dmi_field(candidates)

    # If DMI failed entirely, try `dmidecode -s` as a privileged fallback.
    if not any(inv.get(k) for k in _DMI_FIELDS) and shutil.which("dmidecode"):
        for field, dmi_arg in (
            ("service_tag", "system-serial-number"),
            ("manufacturer", "system-manufacturer"),
            ("model", "system-product-name"),
        ):
            try:
                proc = subprocess.run(
                    ["dmidecode", "-s", dmi_arg],
                    capture_output=True, text=True, timeout=5, shell=False,
                )
                if proc.returncode == 0:
                    inv[field] = proc.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                continue

    # OS info — try host path first so Flatpak/container agents report the real OS
    _os_release: dict = {}
    for _os_rel_path in [Path("/run/host/os-release"), Path("/etc/os-release")]:
        if _os_rel_path.exists():
            try:
                for _line in _os_rel_path.read_text(errors="replace").splitlines():
                    _k, _, _v = _line.partition("=")
                    _os_release[_k.strip()] = _v.strip().strip('"')
                break
            except OSError:
                continue
    inv["os_name"] = (
        _os_release.get("PRETTY_NAME")
        or _os_release.get("NAME", "")
        or f"{platform.system()} {platform.release()}"
    ).strip()
    inv["os_version"] = (_os_release.get("VERSION_ID") or platform.release()).strip()
    inv["kernel_version"] = platform.release()
    inv["architecture"] = platform.machine()

    # Uptime
    try:
        import time as _time_mod
        inv["uptime_seconds"] = int(_time_mod.time() - psutil.boot_time())
    except Exception:
        inv["uptime_seconds"] = 0

    # Last logged-in user
    try:
        _users = psutil.users()
        if _users:
            _last_user = max(_users, key=lambda u: u.started)
            inv["last_logged_user"] = _last_user.name
        else:
            inv["last_logged_user"] = ""
    except Exception:
        inv["last_logged_user"] = ""

    # BIOS info from DMI
    inv["bios_version"] = _read_dmi_field(["bios_version"])
    inv["bios_date"] = _read_dmi_field(["bios_date"])

    # System timezone
    try:
        _tz_path = Path("/etc/timezone")
        if _tz_path.exists():
            inv["system_timezone"] = _tz_path.read_text(errors="replace").strip()
        else:
            import time as _time_mod2
            inv["system_timezone"] = (_time_mod2.tzname or ("",))[0]
    except Exception:
        inv["system_timezone"] = ""

    inv["mac_addresses"] = _read_mac_addresses()
    inv["disks"] = _read_disks()

    return inv


# ---------------------------------------------------------------------------
# Docker image update detection
# ---------------------------------------------------------------------------

_DOCKER_SOCKET = "/var/run/docker.sock"


class _UnixHTTPConnection(_http_client.HTTPConnection):
    """HTTPConnection routed through a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self) -> None:
        import socket as _sock_mod
        self.sock = _sock_mod.socket(_sock_mod.AF_UNIX, _sock_mod.SOCK_STREAM)
        self.sock.connect(self._socket_path)


def _docker_api_get(path: str):
    """GET from the Docker Engine API via the Unix socket. Returns parsed JSON or None."""
    try:
        conn = _UnixHTTPConnection(_DOCKER_SOCKET)
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        if resp.status == 200:
            return _json.loads(resp.read())
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_docker_hub_ref(image_str: str) -> tuple[str, str] | None:
    """Parse a container image string into (docker_hub_name, tag).

    Returns None for non-Docker Hub images (private registries, ghcr.io, etc.).
    Only strings without a registry prefix (i.e. no dot before the first slash,
    or no slash at all) are treated as Docker Hub.
    """
    # Strip digest suffix if present (e.g. nginx@sha256:abc → nginx)
    image_str = image_str.split("@")[0]

    # Split off tag
    if ":" in image_str.rsplit("/", 1)[-1]:
        name_part, tag = image_str.rsplit(":", 1)
    else:
        name_part, tag = image_str, "latest"

    # Detect a registry prefix: first component contains a dot or colon → not Docker Hub
    first_component = name_part.split("/")[0]
    if "." in first_component or ":" in first_component or first_component == "localhost":
        return None

    # No slash → official image (library namespace)
    if "/" not in name_part:
        return f"library/{name_part}", tag

    return name_part, tag


def _get_registry_digest(hub_name: str, tag: str) -> str | None:
    """Return the current manifest digest for hub_name:tag from Docker Hub, or None."""
    try:
        token_resp = requests.get(
            "https://auth.docker.io/token",
            params={"service": "registry.docker.io", "scope": f"repository:{hub_name}:pull"},
            timeout=10,
        )
        token_resp.raise_for_status()
        token = token_resp.json().get("token")
        if not token:
            return None

        resp = requests.get(
            f"https://registry-1.docker.io/v2/{hub_name}/manifests/{tag}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.docker.distribution.manifest.v2+json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.headers.get("Docker-Content-Digest")
    except Exception as exc:
        logger.debug("Registry digest check failed for %s:%s — %s", hub_name, tag, exc)
        return None


def collect_docker_updates() -> list[dict]:
    """Check running Docker containers for outdated images.

    Returns metric dicts for the checkin payload. Skipped silently when Docker
    is unavailable (no socket, permission denied, daemon not running).
    Only Docker Hub public images are checked; private registries are skipped.
    """
    if not Path(_DOCKER_SOCKET).exists():
        return []

    try:
        containers = _docker_api_get("/containers/json")
    except PermissionError:
        logger.warning("Cannot access Docker socket %s — permission denied", _DOCKER_SOCKET)
        return []
    except Exception as exc:
        logger.debug("Docker socket query failed: %s", exc)
        return []

    if not isinstance(containers, list):
        return []

    metrics: list[dict] = [_point("docker", "running_count", float(len(containers)))]

    # One registry query per unique image:tag pair
    digest_cache: dict[str, str | None] = {}
    ts = _now_iso()

    for container in containers:
        image_str = container.get("Image", "")
        ref = _parse_docker_hub_ref(image_str)
        if ref is None:
            logger.debug("Skipping non-Docker-Hub image: %s", image_str)
            continue

        hub_name, tag = ref
        container_name = (container.get("Names") or ["unknown"])[0].lstrip("/")
        image_id = container.get("ImageID", "")

        # Get local manifest digest from image inspect
        image_info = _docker_api_get(f"/images/{image_id}/json") if image_id else None
        repo_digests = (image_info or {}).get("RepoDigests", [])
        if not repo_digests:
            logger.debug("No RepoDigests for %s (%s) — locally built, skipping", container_name, image_str)
            continue
        local_digest = repo_digests[0].split("@")[-1]

        # Registry digest (cached per image:tag)
        cache_key = f"{hub_name}:{tag}"
        if cache_key not in digest_cache:
            digest_cache[cache_key] = _get_registry_digest(hub_name, tag)
        remote_digest = digest_cache[cache_key]

        if remote_digest is None:
            continue  # Registry unreachable — don't emit a metric this cycle

        outdated = 1.0 if local_digest != remote_digest else 0.0
        metrics.append({
            "category": "docker",
            "metric": "image_outdated",
            "value": outdated,
            "labels": {
                "container_name": container_name,
                "image": image_str,
                "local_digest": local_digest[:19],
                "remote_digest": remote_digest[:19],
            },
            "time": ts,
        })
        logger.debug(
            "Docker %s (%s): %s",
            container_name,
            image_str,
            "OUTDATED" if outdated else "up to date",
        )

    return metrics
