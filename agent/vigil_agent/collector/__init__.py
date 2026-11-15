"""The Vigil metrics collector.

One module per concern; this file is the package's public face. Every name any
other module, caller or test imports from ``vigil_agent.collector`` is re-exported
here explicitly, so ``from vigil_agent.collector import x``, ``collector.collect_all(...)``
and a ``patch.object(collector, "...")`` keep resolving exactly as they did when
the collector was a single module.

A patch of a *function* on the package reaches only the caller that looks it up
on the package — a name imported by another module keeps its own binding. Each
such function is therefore called either from the module that owns it (a test
patching ``vigil_agent.collector.<module>.name`` reaches it) or through this
package object (a test patching ``vigil_agent.collector.name`` reaches it); the
docstring of every helper two modules share says which.

The metric dicts each category collector returns all share one shape:
  {"category": str, "metric": str, "value": float, "labels": dict, "time": str}
"""

from ._common import (
    _now_iso,
    _point,
    _warn_once,
    _warned,
    logger,
    psutil,
    shutil,
    subprocess,
    sys,
)
from .disks import _IMAGE_FSTYPES, _is_real_disk
from .basic import collect_cpu, collect_disk, collect_memory, collect_network
from .processes import collect_top_processes
from .temperatures import collect_temperatures
from .gpu import (
    _NVIDIA_FIELDS,
    _ROCM_BYTES,
    _ROCM_KEYS,
    _gpu_float,
    _gpu_memory_percent,
    _rocm_number,
    _run_smi,
    collect_amd_gpu,
    collect_gpu,
    collect_nvidia_gpu,
)
from .aggregate import collect_all
from .identity import (
    _machine_fingerprint_macos,
    _machine_fingerprint_windows,
    _reboot_required_linux,
    _reboot_required_windows,
    machine_fingerprint,
    reboot_required,
)
from .inventory import (
    _DMI_FIELDS,
    _DMI_PATH,
    _detect_os_family,
    _normalize_arch,
    _read_disks,
    _read_dmi_field,
    _read_mac_addresses,
    collect_inventory,
)
from .docker_engine import (
    _DOCKER_SOCKET,
    _UnixHTTPConnection,
    _docker_api_get,
    _engine_available,
)
from .docker_refs import (
    _DIGEST_UNCHANGED,
    _docker_recheck_requested,
    _get_registry_digest,
    _local_hub_digests,
    _normalize_hub_repo,
    _parse_docker_hub_ref,
    consume_docker_recheck,
    request_docker_recheck,
)
from .docker_updates import collect_docker_updates
from .docker_containers import (
    _container_detail,
    _container_mounts,
    _container_networks,
    _docker_container_stats,
    _docker_ports,
    collect_docker_containers,
)
from .docker_resources import collect_docker_resources
