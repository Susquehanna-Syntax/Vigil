import ipaddress
import logging
import secrets
import zoneinfo

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.accounts.permissions import IsAdmin, can, SettingsPage
from apps.metrics.models import MetricPoint

logger = logging.getLogger("apps.hosts.views")  # the name it had before the split


#: Most metrics one check-in may write. An ordinary machine reports ~65 and a
#: large one a few hundred; anything past this is a bug or an attempt.
MAX_METRICS_PER_CHECKIN = 5000

#: Most containers one host may report in a snapshot.
MAX_CONTAINERS_PER_CHECKIN = 500

#: Most label keys kept on a single metric point, and the longest key or value.
#: labels is a JSON column with no schema, so without this a host could store
#: whatever it liked, at whatever size it liked, on every point it wrote.
MAX_LABELS_PER_POINT = 20
MAX_LABEL_LENGTH = 200


def _bounded_labels(raw):
    """The labels an agent reported, trimmed to something storable."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in list(raw.items())[:MAX_LABELS_PER_POINT]:
        out[str(key)[:MAX_LABEL_LENGTH]] = str(value)[:MAX_LABEL_LENGTH]
    return out
from apps.tasks.models import Task
from apps.tasks.spec import schedule_window_active
from vigil import scoping
from vigil.signing import get_public_key_b64, rotation_statement, sign_task, sign_task_v2

from ..auto_tags import merge_auto_tags
from ..authentication import authenticate_agent
from ..crypto import encrypt_secret
from ..enrollment import adopt_enrolment, replacement_candidate
from ..models import (ADConfig, ContainerStack, DockerContainer, Host, HostInventory,
                     HostNetwork, TransportAck, UnmanagedDevice)
from ..serializers import (
    DockerContainerSerializer,
    HostInventorySerializer,
    HostSerializer,
    UnmanagedDeviceSerializer,
)
from ..versions import version_at_least

#: Tokens the installers once wrote for the operator to replace. They are
#: published in the install scripts, so they are credentials in name only.
PLACEHOLDER_AGENT_TOKENS = frozenset({
    "REPLACE_WITH_TOKEN",
    "REPLACE_WITH_SERVER_URL",
})

_MAX_TOKEN_LEN = 255
_MAX_HOSTNAME_LEN = 255


_MAX_TAGS = 32


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Auto-tag namespace — colons keep them visibly distinct from user tags
# and let the UI surface them differently. Any tag the server writes
# here can be rebuilt from inventory, so it's safe to remove + replace
# in this exact prefix list without touching user-set tags.
_AUTO_TAG_PREFIXES = ("os:", "os_family:", "pkg:", "arch:")


def _sync_host_auto_tags(host, inv: dict) -> None:
    """Refresh the colon-namespaced auto-tags on ``host`` from ``inv``.

    Sources, all coming from the agent's collect_inventory():
      * ``host.os`` (set elsewhere from check-in)
      * ``inv["os_family"]`` — "debian" / "rhel" / "alpine" / "macos" / ...
      * ``inv["pkg_manager"]`` — "apt" / "dnf" / "brew" / "winget" / ...
      * ``inv["arch_normalized"]`` — "amd64" / "arm64" / ...

    Strips all existing tags in our namespace, then re-adds whichever
    are non-empty. User tags survive untouched.
    """
    derived: list[str] = []
    if host.os:
        derived.append(f"os:{host.os.strip().lower()}")
    fam = (inv.get("os_family") or "").strip().lower()
    if fam:
        derived.append(f"os_family:{fam}")
    pkg = (inv.get("pkg_manager") or "").strip().lower()
    if pkg:
        derived.append(f"pkg:{pkg}")
    arch = (inv.get("arch_normalized") or "").strip().lower()
    if arch:
        derived.append(f"arch:{arch}")

    user_tags = [
        t for t in (host.tags or [])
        if isinstance(t, str) and not t.startswith(_AUTO_TAG_PREFIXES)
    ]
    new_tags = sorted({*user_tags, *derived})
    if new_tags != (host.tags or []):
        host.tags = new_tags
        host.save(update_fields=["tags"])


# Namespace for tags the agent advertises about itself (agent.yml).
# Deploys can target by tag, so an agent must never be able to mint an
# operator-looking tag like "prod" and opt itself into tag-targeted
# deploys (whose signed params may carry secrets). Everything an agent
# self-reports lands under agent:* where the deploy picker shows its
# provenance.
_AGENT_TAG_PREFIX = "agent:"


def _namespace_agent_tags(raw):
    """Prefix every agent-advertised tag with ``agent:`` (idempotent)."""
    if not isinstance(raw, list):
        return raw
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        t = entry.strip().lower()
        if not t:
            continue
        if not t.startswith(_AGENT_TAG_PREFIX):
            t = _AGENT_TAG_PREFIX + t
        out.append(t)
    return out


def _normalize_tags(raw, *, existing=None):
    """Sanitize incoming tag lists from agents.

    Returns a sorted, deduped list of lowercase tags merged with *existing*
    (server-side tags always win on conflict — operators can't lose tags
    they set in the console). Drops anything that isn't a non-empty string,
    truncates each to 40 chars, and caps the total at _MAX_TAGS.
    """
    if not isinstance(raw, list):
        return list(existing or [])
    out: list[str] = list(existing or [])
    seen = {t for t in out}
    for entry in raw:
        if not isinstance(entry, str):
            continue
        t = entry.strip().lower()[:40]
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= _MAX_TAGS:
            break
    return sorted(out)


class _RegisterRateThrottle(AnonRateThrottle):
    """Per-IP cap on unauthenticated enrollment attempts.

    register() is AllowAny by design (agents have no credentials yet),
    which makes it a free table-bloat vector — anyone who can reach the
    server can mint pending Host rows. 60/hour per source IP is far
    above any legitimate enrollment burst (registration is idempotent;
    an agent registers once per install) while capping abuse.
    """

    rate = "60/hour"


#: Reboot actions that an agent older than 2026.9.0 cannot honour — it
#: drops these params and runs ``shutdown -r now`` instead.
_REBOOT_GATED_PARAMS = {"notify", "defer_limit", "defer_minutes"}
_REBOOT_MIN_AGENT_VERSION = "2026.9.0"


def _normalize_agent_features(raw) -> list[str]:
    """The check-in's ``features``: a list of ≤ 20 short strings.

    Anything else (wrong type, non-strings, oversized entries) is treated as
    an empty list — a corrupt payload must not grant features the agent
    never advertised, and an honest agent's list is tiny.
    """
    if not isinstance(raw, list) or len(raw) > 20:
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, str) or not entry or len(entry) > 40:
            return []
        if entry not in out:
            out.append(entry)
    return out


def _refuse_outdated_reboot(host: Host) -> None:
    """Fail every pending deferral-bearing reboot for a too-old agent.

    Refusing is the only safe option: a 2026.8.0 agent drops
    ``defer_limit``/``defer_minutes``/``notify`` and reboots immediately,
    mid-call. A plain reboot (none of those params) is unaffected.
    """
    if version_at_least(host.agent_version, _REBOOT_MIN_AGENT_VERSION):
        return
    for task in Task.objects.filter(host=host, state=Task.State.PENDING):
        params = task.params or {}
        actions = ([task.action] if task.action else []) + [
            step.get("action") for step in params.get("steps") or []
            if isinstance(step, dict)
        ]
        if "reboot" not in actions:
            continue
        gated = {
            key
            for action_params in ([params] if task.action == "reboot" else [])
            + [s.get("params") or {} for s in params.get("steps") or []
               if isinstance(s, dict) and s.get("action") == "reboot"]
            for key in action_params
            if key in _REBOOT_GATED_PARAMS
        }
        if not gated:
            continue
        task.state = Task.State.FAILED
        task.result_output = (
            f"Refused: agent version {host.agent_version or '(unknown)'} cannot "
            f"honour reboot {', '.join(sorted(gated))} — agent "
            f"{_REBOOT_MIN_AGENT_VERSION} or newer is required"
        )
        task.completed_at = now()
        task.save(update_fields=["state", "result_output", "completed_at"])
        if task.run_id:
            from apps.tasks.views import _advance_run_sequence
            _advance_run_sequence(task)


def _refuse_unsupported_features(host: Host) -> None:
    """Fail every pending task that uses a task-language feature the agent does not understand.

    A task needs ``relevant`` when its params carry a ``relevant:`` block and
    ``branches`` when they carry a ``flow`` tree. An agent that cannot read a
    block would ignore it and run the fix on every host it was sent to —
    exactly the mistake the block exists to prevent — so the server refuses
    to hand it the task instead. Same shape as ``_refuse_outdated_reboot``.
    """
    features = set(host.agent_features or [])
    for task in Task.objects.filter(host=host, state=Task.State.PENDING):
        params = task.params or {}
        needed: list[str] = []
        if isinstance(params.get("relevant"), dict) and "relevant" not in features:
            needed.append("relevant:")
        if params.get("flow") is not None and "branches" not in features:
            needed.append("if/then/else branches")
        if not needed:
            continue
        task.state = Task.State.FAILED
        task.result_output = (
            f"Refused: this agent does not understand {', '.join(needed)} — "
            "update the agent before deploying tasks that use them"
        )
        task.completed_at = now()
        task.save(update_fields=["state", "result_output", "completed_at"])
        if task.run_id:
            from apps.tasks.views import _advance_run_sequence
            _advance_run_sequence(task)


_NO_CONTAINERS = {"containers": 0, "containers_running": 0, "containers_outdated": 0,
                  "stacks": []}


def _container_counts() -> dict:
    """Per host: how many containers, how many running, how many on an
    outdated image, and its compose stacks (M11) — the Inventory columns.
    Three queries for the whole fleet rather than three per host."""
    from django.db.models import Count, Q

    out: dict = {}
    for row in (DockerContainer.objects.values("host_id")
                .annotate(total=Count("id"), running=Count("id", filter=Q(state="running")))):
        out[row["host_id"]] = {"containers": row["total"], "containers_running": row["running"],
                               "containers_outdated": 0, "stacks": []}
    for host_id, _name in _open_outdated():
        if host_id in out:
            out[host_id]["containers_outdated"] += 1
    for stack in ContainerStack.objects.only("host_id", "project", "ownership").order_by("project"):
        entry = out.setdefault(stack.host_id, {**_NO_CONTAINERS, "stacks": []})
        entry["stacks"].append({"project": stack.project, "ownership": stack.ownership})
    return out


def _open_outdated(host_ids=None) -> set:
    """(host_id, container_name) for every open outdated-image alert."""
    from apps.alerts.models import Alert

    qs = Alert.objects.filter(
        rule__name="Docker: Outdated Image",
        state__in=[Alert.State.FIRING, Alert.State.ACKNOWLEDGED],
    )
    if host_ids is not None:
        qs = qs.filter(host_id__in=host_ids)
    return {(a.host_id, (a.fix_context or {}).get("container_name"))
            for a in qs.only("host_id", "fix_context")}


#: Only these may be driven from the Firewall tab. The task editor can express
#: anything in ACTION_REGISTRY; this endpoint deliberately cannot. Must stay
#: exactly in sync with the five action names firewall_guard.check_change
#: recognises — it fails safe and refuses anything else, so drift here would
#: make a legitimate action permanently refused.
_FIREWALL_ACTIONS = {
    "add_firewall_rule", "remove_firewall_rule",
    "set_firewall_policy", "enable_firewall", "disable_firewall",
}


def _firewall_snapshot_dict(fw):
    """Build the plain dict firewall_guard.check_change expects.

    check_change calls .get() on this, so a HostFirewall model instance
    would raise. The guard only reads a few of these keys today but is
    handed the full shape in case it grows to consult more later.
    """
    return {
        "tool": fw.tool,
        "supported": fw.supported,
        "enabled": fw.enabled,
        "defaults": fw.defaults,
        "rules": fw.rules,
        "unparsed": fw.unparsed,
    }


def _queue_firewall_task(host, user, action, params, risk):
    return Task.objects.create(
        host=host, requested_by=user, action=action, params=params or {},
        risk_level=risk, state=Task.State.PENDING,
        nonce=secrets.token_hex(32),
    )


# ── Tag management ──────────────────────────────────────────────────────────
#
# Reads are open to any authenticated user so every tag picker can populate.
# Writes are admin-only: a tag decides which machines a wave patches.


def _visible_tags(user):
    """Global tags and the tags of the sites *user* can see (S1 04h). The
    Tag manager is not site-scoped (names are unique install-wide), so the
    views filter by hand."""
    from ..models import Tag
    return scoping.filter_by_site_field(Tag.objects.all(), user, field="site_id")


def _tag_fits_host(tag, host) -> bool:
    """May *tag* go on *host*: a global tag, or one of the host's own site."""
    owner = scoping.owning_site_id(tag)
    return owner is None or owner == scoping.site_id_of(host)


def _count_everywhere(related_manager) -> int:
    """How many rows a reverse relation names, whatever the site scope."""
    objects = related_manager.model.objects
    if hasattr(objects, "unscoped"):
        qs = objects.unscoped(reason="tag delete guard: a tag is install-wide")
    else:
        qs = objects.all()
    return qs.filter(**related_manager.core_filters).count()


def _tag_payload(tag) -> dict:
    return {
        "id": tag.id,
        "name": tag.name,
        "key": tag.key,
        "kind": tag.kind,
        "description": tag.description,
        "host_count": tag.hosts.count(),
        "editable": tag.kind == tag.Kind.MANUAL,
        "site_id": str(tag.site_id) if scoping.owning_site_id(tag) else None,
    }


def _rename_in_string_mirrors(old_key: str, new_name: str) -> None:
    """Rewrite the string lists that still mirror the rows.

    Rows are authoritative for matching, but the string fields are still the
    write interface and still what the consistency tests compare against, so a
    rename has to land in both or the two drift apart.
    """
    from apps.automations.models import Automation
    from apps.playbooks.models import Playbook
    from apps.reprovision.models import InstallProfile
    from apps.tasks.models import PatchWave

    from ..models import Host

    def rewrite(queryset, field):
        for row in queryset.iterator():
            values = list(getattr(row, field) or [])
            changed = False
            for i, value in enumerate(values):
                if str(value).lower() == old_key:
                    values[i] = new_name
                    changed = True
            if changed:
                setattr(row, field, values)
                row.save()

    rewrite(Host.objects.unscoped(reason="tag rename: tags are install-wide"), "tags")
    rewrite(PatchWave.objects.all(), "tags")
    rewrite(Playbook.objects.unscoped(reason="tag rename: tags are install-wide"), "target_tags")
    every_automation = Automation.objects.unscoped(reason="tag rename: tags are install-wide")
    rewrite(every_automation, "event_tags")
    rewrite(every_automation, "target_tags")
    rewrite(InstallProfile.objects.all(), "completion_tags")


def _edge_list(value, cap: int) -> list:
    """A container's networks or mounts as the agent sent them: a short list
    of flat dicts of short strings (and bools), nothing else."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:cap]:
        if isinstance(item, dict):
            out.append({str(k)[:32]: (v if isinstance(v, bool) else str(v)[:500])
                        for k, v in list(item.items())[:8]
                        if isinstance(v, (str, bool, int))})
    return out


#: Caps per check-in, like MAX_CONTAINERS_PER_CHECKIN: a broken or hostile
#: agent cannot fill the database through these lists.
MAX_DOCKER_OBJECTS = 2000


def _ingest_docker_resources(host, payload: dict) -> None:
    """Replace the host's images, volumes and networks wholesale (14.3)."""
    from datetime import datetime, timezone as _tz

    from ..models import DockerImage, DockerNetwork, DockerVolume

    def items(key):
        value = payload.get(key)
        return [x for x in value[:MAX_DOCKER_OBJECTS] if isinstance(x, dict)] if isinstance(value, list) else None

    images, volumes, networks = items("images"), items("volumes"), items("networks")

    def strs(value, cap, n=16):
        return [str(v)[:cap] for v in value[:n]] if isinstance(value, list) else []

    def created(ts):
        try:
            return datetime.fromtimestamp(int(ts), tz=_tz.utc) if int(ts) > 0 else None
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    with transaction.atomic():
        if images is not None:
            DockerImage.objects.filter(host=host).delete()
            DockerImage.objects.bulk_create([DockerImage(
                host=host, image_id=str(i.get("id") or "")[:80], tags=strs(i.get("tags"), 255),
                digests=strs(i.get("digests"), 300, 4), size_bytes=_safe_int(i.get("size")) or 0,
                created=created(i.get("created"))) for i in images])
        if volumes is not None:
            DockerVolume.objects.filter(host=host).delete()
            DockerVolume.objects.bulk_create([DockerVolume(
                host=host, name=str(v.get("name") or "")[:255], driver=str(v.get("driver") or "")[:64],
                mountpoint=str(v.get("mountpoint") or "")[:500], created=str(v.get("created") or "")[:40],
                stack=str(v.get("stack") or "")[:200]) for v in volumes if v.get("name")])
        if networks is not None:
            DockerNetwork.objects.filter(host=host).delete()
            DockerNetwork.objects.bulk_create([DockerNetwork(
                host=host, network_id=str(n.get("id") or "")[:64], name=str(n.get("name") or "")[:255],
                driver=str(n.get("driver") or "")[:64], scope=str(n.get("scope") or "")[:16],
                internal=n.get("internal") is True, subnets=strs(n.get("subnets"), 64, 8),
                stack=str(n.get("stack") or "")[:200]) for n in networks if n.get("name")])


#: Caps per check-in for the network snapshot (14.4), the same reasoning as
#: MAX_DOCKER_OBJECTS: a broken or hostile agent cannot fill the database
#: through these lists. A machine with 32 interfaces or 300 aggregates is
#: already far past an ordinary one.
MAX_NETWORK_INTERFACES = 32
MAX_NETWORK_ADDR = 16
MAX_NETWORK_LISTENING = 256
MAX_NETWORK_CONNECTIONS = 300
NET_PROTOS = {"tcp", "udp"}
NET_DIRECTIONS = {"in", "out"}


def _net_ip(value):
    """An address the ingest will store, or None if it isn't one. Keeps the
    canonical form, so the topology view joins on the same text everywhere."""
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return None


def _net_port(value):
    port = _safe_int(value)
    return port if port is not None and 0 <= port <= 65535 else None


def _net_items(value, cap: int) -> list:
    if not isinstance(value, list):
        return []
    return [x for x in value[:cap] if isinstance(x, dict)]


def _ingest_network(host, payload: dict) -> None:
    """Store this host's network snapshot (14.4): what it is called, what it
    listens on, who it talks to. One row per host, replaced on arrival."""
    interfaces = []
    for iface in _net_items(payload.get("interfaces"), MAX_NETWORK_INTERFACES):
        addrs = []
        for addr in _net_items(iface.get("addrs"), MAX_NETWORK_ADDR):
            address = _net_ip(addr.get("address"))
            if address is None:
                continue
            prefix = _safe_int(addr.get("prefix"))
            if prefix is not None and not 0 <= prefix <= 128:
                prefix = None
            addrs.append({"family": str(addr.get("family") or "")[:8],
                          "address": address, "prefix": prefix})
        interfaces.append({"name": str(iface.get("name") or "")[:64],
                           "up": iface.get("up") is True,
                           "mac": str(iface.get("mac") or "")[:32],
                           "addrs": addrs})

    listening = []
    for row in _net_items(payload.get("listening"), MAX_NETWORK_LISTENING):
        proto = str(row.get("proto") or "")
        address = _net_ip(row.get("address"))
        port = _net_port(row.get("port"))
        if proto not in NET_PROTOS or address is None or port is None:
            continue
        listening.append({"proto": proto, "address": address, "port": port,
                          "process": str(row.get("process") or "")[:64]})

    connections = []
    for row in _net_items(payload.get("connections"), MAX_NETWORK_CONNECTIONS):
        proto = str(row.get("proto") or "")
        direction = str(row.get("direction") or "")
        remote_ip = _net_ip(row.get("remote_ip"))
        remote_port = _net_port(row.get("remote_port"))
        local_port = _net_port(row.get("local_port"))
        if (proto not in NET_PROTOS or direction not in NET_DIRECTIONS
                or remote_ip is None or remote_port is None or local_port is None):
            continue
        count = _safe_int(row.get("count"))
        if count is None or count < 1:
            count = 1
        connections.append({"proto": proto, "direction": direction,
                            "remote_ip": remote_ip, "remote_port": remote_port,
                            "local_port": local_port,
                            "process": str(row.get("process") or "")[:64],
                            "count": min(count, 100000)})

    HostNetwork.objects.update_or_create(
        host=host,
        defaults={"interfaces": interfaces, "listening": listening,
                  "connections": connections, "partial": payload.get("partial") is True,
                  "collected_at": now()})


def _sync_container_stacks(host, containers: list) -> None:
    """Upsert the host's compose stacks from its containers' labels (M11).

    A stack no container belongs to any more is dropped — unless Vigil manages
    or adopted it, in which case it stays (stopped is not gone).
    """
    engines = host.container_engines or []
    engine = engines[0]["kind"] if engines else ""
    found: dict = {}
    for c in containers:
        if not isinstance(c, dict) or not c.get("stack"):
            continue
        project = str(c["stack"])[:200]
        entry = found.setdefault(project, {"config_files": [], "working_dir": "",
                                           "count": 0, "running": 0})
        entry["count"] += 1
        entry["running"] += 1 if str(c.get("state") or "") == "running" else 0
        files = [f.strip() for f in str(c.get("config_files") or "").split(",") if f.strip()]
        if files and not entry["config_files"]:
            entry["config_files"] = files[:10]
        if c.get("working_dir") and not entry["working_dir"]:
            entry["working_dir"] = str(c["working_dir"])[:500]
    stamp = now()
    from apps.stacks.models import ManagedStack
    # A project Vigil holds the compose file for is Vigil's, not "external",
    # however its containers were started.
    managed = dict(ManagedStack.objects.filter(host=host, name__in=list(found))
                   .values_list("name", "adopted"))
    for project, entry in found.items():
        stack, created = ContainerStack.objects.get_or_create(
            host=host, project=project,
            defaults={"config_files": entry["config_files"], "working_dir": entry["working_dir"],
                      "engine": engine})
        if project in managed and stack.ownership == ContainerStack.Ownership.EXTERNAL:
            stack.ownership = (ContainerStack.Ownership.ADOPTED if managed[project]
                               else ContainerStack.Ownership.MANAGED)
        stack.container_count, stack.running_count = entry["count"], entry["running"]
        stack.seen_at = stamp
        if entry["config_files"] and stack.ownership == ContainerStack.Ownership.EXTERNAL:
            stack.config_files, stack.working_dir = entry["config_files"], entry["working_dir"]
        stack.engine = engine or stack.engine
        stack.save()
    (ContainerStack.objects.filter(host=host, ownership=ContainerStack.Ownership.EXTERNAL)
     .exclude(project__in=list(found)).delete())
    (ContainerStack.objects.filter(host=host).exclude(project__in=list(found))
     .update(container_count=0, running_count=0))


# Every name here, private ones included, for the view modules' star import.
__all__ = ["ADConfig", "AllowAny", "AnonRateThrottle", "ContainerStack", "DockerContainer", "DockerContainerSerializer", "Host", "HostInventory", "HostInventorySerializer", "HostNetwork", "HostSerializer", "IsAdmin", "IsAuthenticated", "MAX_CONTAINERS_PER_CHECKIN", "MAX_DOCKER_OBJECTS", "MAX_LABELS_PER_POINT", "MAX_LABEL_LENGTH", "MAX_METRICS_PER_CHECKIN", "MAX_NETWORK_ADDR", "MAX_NETWORK_CONNECTIONS", "MAX_NETWORK_INTERFACES", "MAX_NETWORK_LISTENING", "MetricPoint", "NET_DIRECTIONS", "NET_PROTOS", "PLACEHOLDER_AGENT_TOKENS", "Response", "SettingsPage", "Task", "TransportAck", "UnmanagedDevice", "UnmanagedDeviceSerializer", "_AGENT_TAG_PREFIX", "_AUTO_TAG_PREFIXES", "_FIREWALL_ACTIONS", "_MAX_HOSTNAME_LEN", "_MAX_TAGS", "_MAX_TOKEN_LEN", "_NO_CONTAINERS", "_REBOOT_GATED_PARAMS", "_REBOOT_MIN_AGENT_VERSION", "_RegisterRateThrottle", "_bounded_labels", "_container_counts", "_count_everywhere", "_edge_list", "_firewall_snapshot_dict", "_ingest_docker_resources", "_ingest_network", "_namespace_agent_tags", "_net_ip", "_net_items", "_net_port", "_normalize_agent_features", "_normalize_tags", "_open_outdated", "_queue_firewall_task", "_refuse_outdated_reboot", "_refuse_unsupported_features", "_rename_in_string_mirrors", "_safe_float", "_safe_int", "_sync_container_stacks", "_sync_host_auto_tags", "_tag_fits_host", "_tag_payload", "_visible_tags", "adopt_enrolment", "api_view", "authenticate_agent", "can", "encrypt_secret", "get_object_or_404", "get_public_key_b64", "ipaddress", "logger", "logging", "merge_auto_tags", "now", "parse_datetime", "permission_classes", "replacement_candidate", "rotation_statement", "schedule_window_active", "scoping", "secrets", "sign_task", "sign_task_v2", "status", "throttle_classes", "transaction", "version_at_least", "zoneinfo"]
