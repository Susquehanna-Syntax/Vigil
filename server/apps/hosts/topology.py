"""One read-only API for the topology page (14.4): hosts, addresses, ports,
firewalls, stacks and the links between them.

Everything the map draws comes from one response, so the page does not fan out
into a request per host. Hosts without a network snapshot — an agent older than
2026.14.4 — are still listed: a map that silently dropped machines would look
healthy and be wrong.

Docker is deliberately half-in. A bridge address is still the *machine's*
address: a published container port is answered by the host, so a line to
172.17.0.1 is a line to the host and must not become a link to itself. What it
never is, is a drawn segment (§9) — docker's private pools are per-host
plumbing, not a network anyone crosses.
"""
import ipaddress
from collections import defaultdict

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from vigil import scoping

from .models import DockerContainer, Host, UnmanagedDevice

MAX_LISTENING = 64
MAX_ALLOW_PORTS = 32
MAX_LINK_PORTS = 16
MAX_EXTERNAL = 10
MAX_EXTERNAL_PORTS = 8

#: The pool docker pulls per-network subnets from (docker's default: the static
#: bridge at 172.17/16 plus whatever the ipam hands out of 172.16/12). Both are
#: docker *defaults* a site can reconfigure, so the interface name is the
#: primary signal and this pool only catches an address reported without one.
DOCKER_POOL = ipaddress.ip_network("172.16.0.0/12")
DOCKER_IFACE_PREFIXES = ("docker", "br-", "veth", "cni", "flannel", "cali")

#: A subnet worth drawing: /31 point-to-point links and /32 host routes are noise.
SUBNET_MIN_PREFIX, SUBNET_MAX_PREFIX = 16, 30


def build_topology(user) -> dict:
    """The whole map: sites, tags, subnets, hosts, devices and links."""
    hosts = list(_visible_hosts(user))
    host_ids = [h.id for h in hosts]

    containers = defaultdict(list)
    for container in DockerContainer.objects.filter(host__in=host_ids):
        containers[container.host_id].append(container)

    devices = _devices()

    # One pass over the interface data feeds three things: every address the
    # host has, the subset that identifies it as a destination, and the subnets.
    addresses, ips, claims = {}, {}, {}
    for host in hosts:
        addr_list = _host_addresses(host)
        addresses[host.id] = addr_list
        ips[host.id] = _ip_list(addr_list, host.ip_address)
        claims[host.id] = _claims(addr_list, host.ip_address)
    owners = _owners(claims, devices)
    subnets = _subnets(addresses)

    site_of = scoping.sites_for_hosts(host_ids)
    tag_names = _tag_names(hosts)
    host_rows, per_link = [], {}

    for host in hosts:
        host_links, external = _edges(host, owners)
        for key, entry in host_links.items():
            existing = per_link.get(key)
            if existing is None:
                per_link[key] = entry
            else:
                existing["ports"] |= entry["ports"]
                for session, count in entry["counts"].items():
                    existing["counts"][session] = max(existing["counts"].get(session, 0), count)
        site = site_of.get(host.id)
        network = getattr(host, "network", None)
        host_rows.append({
            "id": str(host.id),
            "hostname": host.hostname,
            "status": host.status,
            "os": host.os,
            "site_id": str(site.id) if site is not None else None,
            "tags": _host_tags(host, tag_names),
            "ips": ips[host.id],
            "primary_ip": _primary_ip(host.ip_address, ips[host.id]),
            "listening": _listening(host),
            "firewall": _firewall(host),
            "stacks": _stacks(containers.get(host.id, [])),
            "external": external,
            "network_at": network.collected_at.isoformat() if network else None,
            "partial": bool(network.partial) if network else False,
        })

    links = [{"source": source, "target": target,
              "ports": sorted(entry["ports"])[:MAX_LINK_PORTS],
              "count": sum(entry["counts"].values())}
             for (source, target), entry in per_link.items()]

    return {
        "sites": _sites(site_of),
        "tags": _tags(hosts, tag_names),
        "subnets": subnets,
        "hosts": host_rows,
        "devices": devices,
        "links": sorted(links, key=lambda link: (link["source"], link["target"])),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def topology(request):
    """`GET /api/v1/hosts/topology/` — the topology page's only request."""
    return Response(build_topology(request.user))


# ── internals ───────────────────────────────────────────────────────────────


def _visible_hosts(user):
    return (scoping.filter_by_site(
        Host.objects.exclude(status__in=(Host.Status.REJECTED, Host.Status.PENDING)), user)
        # The reverse one-to-one accessor form: "network" and "firewall" are
        # what the rows below read, so host.network / host.firewall are free.
        .select_related("network", "firewall")
        .order_by("hostname"))


def _devices():
    """Every registered device with an address.

    The registry is global — UnmanagedDevice has no site assignment, so there
    is nothing to scope here — and a device nobody has given an address yet is
    the only one that cannot be placed on the map.

    Not ``exclude(ip_address="")``: on SQLite a GenericIPAddressField compares
    that empty string to ``None``, which would filter out every row.
    """
    return [{"id": str(d.id), "name": d.name, "type": d.device_type, "ip": str(d.ip_address)}
            for d in UnmanagedDevice.objects.exclude(ip_address__isnull=True).order_by("name")]


def _sites(site_of):
    """Sites that actually have a host here, alphabetically."""
    unique = {}
    for site in site_of.values():
        unique[str(site.id)] = {"id": str(site.id), "name": site.name}
    return sorted(unique.values(), key=lambda s: s["name"].lower())


def _tag_names(hosts):
    """Tags match without regard to case, as they do everywhere else in Vigil:
    "Canary" and "canary" are one tag, shown in its most common spelling."""
    spellings: dict = defaultdict(lambda: defaultdict(int))
    for host in hosts:
        for tag in host.tags or []:
            if isinstance(tag, str) and tag.strip():
                spellings[tag.strip().casefold()][tag.strip()] += 1
    return {key: sorted(forms.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            for key, forms in spellings.items()}


def _host_tags(host, tag_names):
    out = []
    for tag in host.tags or []:
        if isinstance(tag, str) and tag.strip():
            name = tag_names[tag.strip().casefold()]
            if name not in out:
                out.append(name)
    return out


def _tags(hosts, tag_names):
    counts: dict = defaultdict(int)
    for host in hosts:
        for tag in _host_tags(host, tag_names):
            counts[tag] += 1
    return [{"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _ip(text):
    try:
        return ipaddress.ip_address(str(text).strip())
    except ValueError:
        return None


def _port(text):
    try:
        port = int(text)
    except (TypeError, ValueError):
        return None
    return port if 0 < port <= 65535 else None


def _is_docker_interface(name) -> bool:
    return str(name or "").lower().startswith(DOCKER_IFACE_PREFIXES)


def _host_addresses(host):
    """[(interface name, address, prefix)] from the snapshot, in report order."""
    network = getattr(host, "network", None)
    out = []
    for iface in (network.interfaces or []) if network else []:
        if not isinstance(iface, dict):
            continue
        name = str(iface.get("name") or "")
        for addr in iface.get("addrs") or []:
            if not isinstance(addr, dict):
                continue
            parsed = _ip(addr.get("address"))
            if parsed is None:
                continue
            prefix = addr.get("prefix")
            out.append((name, parsed, prefix if isinstance(prefix, int) else None))
    return out


def _ip_list(addr_list, host_ip) -> list:
    """Every address the host reports, plus the one the server saw it at;
    IPv4 first so a dual-stack machine's map label is the address a user
    recognises."""
    seen, v4, v6 = set(), [], []
    for _, addr, _prefix in addr_list:
        if str(addr) not in seen:
            seen.add(str(addr))
            (v4 if addr.version == 4 else v6).append(str(addr))
    if host_ip and str(host_ip) not in seen:
        seen.add(str(host_ip))
        (v4 if _ip(host_ip).version == 4 else v6).append(str(host_ip))
    return v4 + v6


def _claims(addr_list, host_ip) -> set:
    """The addresses that identify this host as a *destination*.

    A veth's address belongs to a container: the container is reached through
    its host, so a line drawn to a veth address would point at a machine that
    is not on the map. Everything else the host reports is this machine, and so
    is ``Host.ip_address`` — the address the fleet sees it at, NAT or no NAT —
    which is why a socket whose source address is one of ours is never drawn as
    a link from this machine to itself.
    """
    claims = {str(addr) for name, addr, _prefix in addr_list
              if not str(name).lower().startswith("veth")}
    if host_ip:
        claims.add(str(host_ip))
    return claims


def _primary_ip(host_ip, ips):
    if host_ip:
        return str(host_ip)
    for text in ips:
        addr = _ip(text)
        if isinstance(addr, ipaddress.IPv4Address) and not addr.is_loopback \
                and not addr.is_link_local:
            return text
    return _global_v6(ips)


def _global_v6(ips):
    """After a routable IPv4, a global IPv6 beats a unique-local one: fc00::/7
    and fe80:: are not addresses a user can reach the machine at."""
    for text in ips:
        addr = _ip(text)
        if isinstance(addr, ipaddress.IPv6Address) \
                and not (addr.is_loopback or addr.is_link_local or addr.is_private
                         or addr.is_multicast or addr.is_unspecified):
            return text
    return ips[0] if ips else None


def _listening(host):
    """(proto, port) pairs, public when anything binds outside loopback."""
    network = getattr(host, "network", None)
    by_port: dict = {}
    for row in (network.listening or []) if network else []:
        if not isinstance(row, dict):
            continue
        port = _port(row.get("port"))
        if port is None:
            continue
        proto = str(row.get("proto") or "")[:8]
        entry = by_port.setdefault((proto, port), {"proto": proto, "port": port,
                                                   "process": "", "public": False})
        address = _ip(row.get("address"))
        if address is not None and not address.is_loopback:
            entry["public"] = True
        if not entry["process"]:
            entry["process"] = str(row.get("process") or "")[:64]
    return [entry for entry in sorted(by_port.values(), key=lambda e: (e["port"], e["proto"]))
            [:MAX_LISTENING]]


def _firewall(host):
    row = getattr(host, "firewall", None)
    if row is None:
        return None
    defaults = row.defaults if isinstance(row.defaults, dict) else {}
    rules = row.rules if isinstance(row.rules, list) else []
    allows = set()
    for rule in rules:
        if not isinstance(rule, dict) or str(rule.get("action") or "").lower() != "allow":
            continue
        port = _port(rule.get("port"))
        if port is not None:
            allows.add(port)
    return {"tool": row.tool, "enabled": row.enabled,
            "incoming": defaults.get("incoming", ""),
            "rules": len(rules),
            "allows": sorted(allows)[:MAX_ALLOW_PORTS]}


def _stacks(containers):
    """Containers grouped by compose project; unnamed ones in a group named
    "", which sorts last so the named stacks come first."""
    groups: dict = defaultdict(list)
    for container in containers:
        ports = [p for p in container.ports or [] if isinstance(p, dict)]
        groups[container.stack or ""].append({
            "name": container.name,
            "state": container.state,
            "ports": [f"{p.get('private')}/{p.get('type') or 'tcp'}" for p in ports],
            "published": sorted({port for port in (_port(p.get("public")) for p in ports)
                                 if port is not None}),
            "networks": [str(n.get("name")) for n in container.networks or []
                         if isinstance(n, dict) and n.get("name")],
        })
    for members in groups.values():
        members.sort(key=lambda c: c["name"].lower())
    names = sorted(groups, key=lambda name: (name == "", name.lower()))
    return [{"name": name, "containers": groups[name]} for name in names]


def _owners(claims, devices):
    """{ip: ("host"|"device", id)} — who a connection's remote address is.

    A host claims an address by reporting it as its own (see _claims), which is
    also how a veth address stays out: it is a container's, not a machine's.
    The hand-typed device registry never outranks a machine talking about its
    own interfaces. Among hosts the first claim wins — two machines reporting
    one address is a conflict we cannot resolve, and report order is at least
    deterministic.
    """
    owners: dict = {}
    for host_id, host_claims in claims.items():
        for addr in sorted(host_claims):
            owners.setdefault(addr, ("host", str(host_id)))
    for device in devices:
        owners.setdefault(device["ip"], ("device", device["id"]))
    return owners


def _subnets(addresses):
    """IPv4 networks the fleet has an address in.

    Docker's own plumbing is not a segment: an address on a docker, br-, veth,
    cni-, flannel- or cali- interface is a private per-host network rather than
    a network anyone crosses. The 172.16/12 pool is docker's *default* range and
    a site can put its LAN there, so it only disqualifies an address the agent
    reported without an interface name to go by.
    """
    hosts_in: dict = defaultdict(set)
    for host_id, addr_list in addresses.items():
        for iface_name, addr, prefix in addr_list:
            if not isinstance(addr, ipaddress.IPv4Address) \
                    or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
                continue
            if prefix is None or not SUBNET_MIN_PREFIX <= prefix <= SUBNET_MAX_PREFIX:
                continue
            if _is_docker_interface(iface_name) \
                    or (not iface_name and addr in DOCKER_POOL):
                continue
            try:
                network = ipaddress.ip_interface(f"{addr}/{prefix}").network
            except ValueError:
                continue
            hosts_in[str(network)].add(str(host_id))
    return [{"cidr": cidr, "host_ids": sorted(hosts_in[cidr])}
            for cidr in sorted(hosts_in, key=lambda c: ipaddress.ip_network(c))]


def _edges(host, owners):
    """This host's links to other managed machines, and what it reaches
    outside the fleet.

    A TCP session is reported by both of its ends, and the two rows describe
    one socket with *different* port numbers: the server end knows the service
    port, the client end only its own ephemeral one. So the port drawn is the
    service port — the only one that survives the round trip — and the socket
    itself is keyed by (service, ephemeral), which both ends agree on.
    """
    network = getattr(host, "network", None)
    me = ("host", str(host.id))
    links: dict = {}
    external: dict = defaultdict(lambda: {"count": 0, "ports": set()})

    for row in (network.connections or []) if network else []:
        if not isinstance(row, dict):
            continue
        remote = str(row.get("remote_ip") or "")
        try:
            count = max(1, int(row.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        owner = owners.get(remote)
        if owner is not None and owner != me:
            direction = str(row.get("direction") or "")
            service, ephemeral = _service_ports(direction, row)
            source, target = (owner, me) if direction == "in" else (me, owner)
            entry = links.setdefault((str(source[1]), str(target[1])),
                                     {"ports": set(), "counts": {}})
            if service is not None:
                entry["ports"].add(service)
            # Both ends of one socket land on the same key, so their identical
            # counts merge; distinct sockets of one service have distinct
            # ephemeral ports, so a real fan-out still adds up.
            session = (service, ephemeral)
            entry["counts"][session] = max(entry["counts"].get(session, 0), count)
            continue
        address = _ip(remote)
        if address is None or address.is_loopback:
            continue
        bucket = external[remote]
        bucket["count"] += count
        port = _port(row.get("remote_port"))
        if port is not None:
            bucket["ports"].add(port)

    externals = [{"ip": ip, "count": row["count"],
                  "ports": sorted(row["ports"])[:MAX_EXTERNAL_PORTS]}
                 for ip, row in external.items()]
    externals.sort(key=lambda e: (-e["count"], e["ip"]))
    return links, externals[:MAX_EXTERNAL]


def _service_ports(direction: str, row: dict):
    """(the port to draw, this socket's ephemeral port).

    An inbound row's remote port is the peer's ephemeral one and its local port
    is the service; an outbound row is the mirror image.
    """
    remote = _port(row.get("remote_port"))
    local = _port(row.get("local_port"))
    if str(direction or "") == "in":
        return local, remote
    return remote, local
