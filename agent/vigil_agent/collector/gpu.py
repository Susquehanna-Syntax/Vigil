"""GPU telemetry from nvidia-smi and rocm-smi.

Neither SMI tool is installed on most hosts, so absence is a silent empty
result rather than an error or a per-scrape log line. Both vendors are tried: a
workstation can hold cards from both.
"""

import json as _json
import re

from ..procenv import clean_env
from ._common import _point, logger, shutil, subprocess


def collect_gpu(extended: bool = False) -> list[dict]:
    """Every GPU this host can see, from either vendor.

    Both tools are tried: a workstation can hold cards from both, and neither
    tool being installed is the ordinary case rather than a failure.
    """
    points = []
    for fn in (collect_nvidia_gpu, collect_amd_gpu):
        try:
            points.extend(fn(extended))
        except Exception:
            logger.exception("GPU collector %s failed", fn.__name__)
    return points



#: nvidia-smi query fields → (metric name, whether it is part of the wide set).
#: Order matters: it is the order the CSV comes back in.
_NVIDIA_FIELDS = [
    ("utilization.gpu", "utilization_percent", False),
    ("memory.used", "memory_used_mb", False),
    ("memory.total", "memory_total_mb", False),
    ("temperature.gpu", "temperature_celsius", False),
    ("power.draw", "power_watts", False),
    ("fan.speed", "fan_percent", True),
    ("clocks.current.graphics", "clock_graphics_mhz", True),
    ("clocks.current.memory", "clock_memory_mhz", True),
    ("utilization.memory", "memory_bandwidth_percent", True),
    ("pcie.link.gen.current", "pcie_generation", True),
    ("pcie.link.width.current", "pcie_width", True),
    ("ecc.errors.corrected.volatile.total", "ecc_corrected", True),
    ("ecc.errors.uncorrected.volatile.total", "ecc_uncorrected", True),
]


def _gpu_float(raw: str) -> float | None:
    """nvidia-smi writes [N/A], [Not Supported] and blanks for absent fields."""
    text = (raw or "").strip()
    if not text or text.startswith("["):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _run_smi(argv: list[str], timeout: int = 10) -> str | None:
    """Run a vendor SMI tool, or return None if it is absent or unhappy.

    A host with no GPU is the common case, so absence is not an error and is
    not logged — every scrape would log it.
    """
    if not shutil.which(argv[0]):
        return None
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=clean_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("%s failed: %s", argv[0], exc)
        return None
    if proc.returncode != 0:
        logger.warning("%s exited %s: %s", argv[0], proc.returncode,
                       (proc.stderr or "").strip()[:200])
        return None
    return proc.stdout


def collect_nvidia_gpu(extended: bool = False) -> list[dict]:
    """Per-GPU metrics from nvidia-smi. Empty list when there is no NVIDIA GPU."""
    fields = [f for f in _NVIDIA_FIELDS if extended or not f[2]]
    query = ",".join(["index", "name"] + [f[0] for f in fields])
    out = _run_smi([
        "nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits",
    ])
    if not out:
        return []
    points = []
    for line in out.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(fields) + 2:
            logger.warning("nvidia-smi row has %s cells, expected %s",
                           len(cells), len(fields) + 2)
            continue
        labels = {"vendor": "nvidia", "index": cells[0], "name": cells[1]}
        for (_, metric, _wide), raw in zip(fields, cells[2:]):
            value = _gpu_float(raw)
            if value is not None:
                points.append(_point("gpu", metric, value, labels))
    # Percent used is what an operator reads; derive it rather than making the
    # dashboard divide two series that may not have arrived together.
    return points + _gpu_memory_percent(points)


def _gpu_memory_percent(points: list[dict]) -> list[dict]:
    used, total = {}, {}
    for pt in points:
        key = (pt["labels"].get("vendor"), pt["labels"].get("index"))
        if pt["metric"] == "memory_used_mb":
            used[key] = (pt["value"], pt["labels"])
        elif pt["metric"] == "memory_total_mb":
            total[key] = pt["value"]
    out = []
    for key, (value, labels) in used.items():
        cap = total.get(key) or 0
        if cap > 0:
            out.append(_point("gpu", "memory_percent", 100.0 * value / cap, labels))
    return out


#: rocm-smi --json key → metric. ROCm's key names have moved between releases
#: and carry the card index inside them, so each metric lists the spellings
#: seen in the wild rather than one exact key.
_ROCM_KEYS = [
    (("GPU use (%)", "GPU use (%) ", "gpu_use_percentage"), "utilization_percent", False),
    (("VRAM Total Used Memory (B)", "vram_used"), "memory_used_mb", False),
    (("VRAM Total Memory (B)", "vram_total"), "memory_total_mb", False),
    (("Temperature (Sensor edge) (C)", "Temperature (Sensor junction) (C)",
      "temp_edge"), "temperature_celsius", False),
    (("Average Graphics Package Power (W)", "Current Socket Graphics Package Power (W)",
      "power"), "power_watts", False),
    (("Fan speed (%)", "fan_speed_percent"), "fan_percent", True),
    (("sclk clock speed:", "sclk"), "clock_graphics_mhz", True),
    (("mclk clock speed:", "mclk"), "clock_memory_mhz", True),
    (("pcie_bw", "PCIe Replay Count"), "pcie_replay_count", True),
]

#: Byte-valued ROCm keys reported in MB, to match the NVIDIA series.
_ROCM_BYTES = {"memory_used_mb", "memory_total_mb"}


def _rocm_number(raw) -> float | None:
    """ROCm reports numbers as strings, sometimes with a unit suffix."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text or text.upper() in {"N/A", "NONE", "NOT SUPPORTED"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def collect_amd_gpu(extended: bool = False) -> list[dict]:
    """Per-GPU metrics from rocm-smi. Empty list when there is no AMD GPU."""
    out = _run_smi(["rocm-smi", "--showallinfo", "--json"])
    if not out:
        return []
    try:
        data = _json.loads(out)
    except ValueError:
        logger.warning("rocm-smi did not return JSON")
        return []
    if not isinstance(data, dict):
        return []
    points = []
    for card, values in data.items():
        if not isinstance(values, dict):
            continue
        index = card.replace("card", "").strip() or card
        labels = {"vendor": "amd", "index": index,
                  "name": str(values.get("Card series")
                              or values.get("Card model") or card)}
        for keys, metric, wide in _ROCM_KEYS:
            if wide and not extended:
                continue
            raw = next((values[k] for k in keys if k in values), None)
            value = _rocm_number(raw)
            if value is None:
                continue
            if metric in _ROCM_BYTES:
                value = value / (1024 * 1024)
            points.append(_point("gpu", metric, value, labels))
    return points + _gpu_memory_percent(points)


