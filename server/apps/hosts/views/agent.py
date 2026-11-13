"""apps.hosts.views: agent views (split from the old single module, unchanged)."""
from drf_spectacular.utils import extend_schema

from ..api_schema import (HostsCheckinBodySerializer, HostsCheckinSerializer,
                          HostsCheckPendingSerializer, HostsCheckPendingTokenSerializer,
                          HostsErrorSerializer, HostsHostSerializer,
                          HostsRegisterResponseSerializer, HostsRegisterSerializer,
                          HostsTransportAckBodySerializer, HostsTransportAckSerializer)
from ._common import *  # noqa: F401,F403


@scoping.system_view(reason="agent enrolment: a new host registers with no user")
@extend_schema(request=HostsRegisterSerializer, responses={
    201: HostsRegisterResponseSerializer, 200: HostsRegisterResponseSerializer,
    400: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([_RegisterRateThrottle])
def register(request):
    """Register a new agent. Creates a pending Host awaiting admin approval."""
    token = request.data.get("agent_token", "").strip()
    hostname = request.data.get("hostname", "").strip()

    if not token or not hostname:
        return Response(
            {"error": "agent_token and hostname are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if len(token) > _MAX_TOKEN_LEN or len(hostname) > _MAX_HOSTNAME_LEN:
        return Response(
            {"error": "Field value too long"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..models import TOKEN_RE
    if not TOKEN_RE.match(token):
        # Tokens are stored hashed; one shaped like anything else (a stored
        # hash included) is refused rather than guessed at.
        return Response({"error": "agent_token must be 16-128 letters, digits, '-' or '_'"},
                        status=status.HTTP_400_BAD_REQUEST)

    machine_id = str(request.data.get("machine_id") or "").strip()[:200]

    # The installers used to leave this literal string in agent.yml for the
    # operator to replace. Since the server stores whatever token an agent
    # presents, an agent started before that edit authenticated with a value
    # published in the install script itself — and because this endpoint is
    # idempotent on the token, a second machine presenting it inherited the
    # first host's already-approved identity, with no admin action and no new
    # pending record. Refusing it here closes that path.
    #
    # Deliberately only on registration: an existing host stuck on this token
    # keeps checking in and keeps being monitored. Breaking ingest to punish a
    # weak credential would be the worse failure, and rotating it is an
    # operator action, not something to force mid-flight.
    if token in PLACEHOLDER_AGENT_TOKENS:
        logger.warning(
            "Refused registration for %r using a placeholder agent token. "
            "Reinstall the agent so the installer generates one.", hostname,
        )
        return Response(
            {
                "error": (
                    "That agent token is the installer's placeholder, which is "
                    "public. Reinstall the agent so a real token is generated, "
                    "or set one explicitly via VIGIL_TOKEN."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Idempotent: if the token already exists, return current status
    existing = Host.objects.by_token(token).first()
    if existing:
        # An agent upgraded into this release backfills its own row on next
        # start; a changed machine_id means this token now belongs to another
        # machine and the newer fingerprint wins.
        if machine_id and existing.machine_id != machine_id:
            existing.machine_id = machine_id
            existing.save(update_fields=["machine_id"])
        return Response(
            {"id": str(existing.id), "status": existing.status},
            status=status.HTTP_200_OK,
        )

    seed_tags = _normalize_tags(_namespace_agent_tags(request.data.get("tags")))

    host = Host.objects.create(
        hostname=hostname,
        os=request.data.get("os", "")[:100],
        kernel=request.data.get("kernel", "")[:100],
        ip_address=request.META.get("REMOTE_ADDR"),
        agent_token=token,
        machine_id=machine_id,
        status=Host.Status.PENDING,
        tags=seed_tags,
    )
    return Response(
        {"id": str(host.id), "status": host.status},
        status=status.HTTP_201_CREATED,
    )


@scoping.system_view(reason="agent API: the caller is a host, authenticated by its agent token")
@extend_schema(methods=["POST"], request=HostsCheckinBodySerializer,
               responses={200: HostsCheckinSerializer, 401: HostsErrorSerializer,
                          403: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([AllowAny])
def checkin(request):
    # A pending host checks in to reach the enrolment queue; rejected hosts
    # are answered just below.
    host, err = authenticate_agent(request, allow_unapproved=True)
    if err:
        return err

    if host.status == Host.Status.REJECTED:
        return Response(
            {"error": "Host enrollment has been rejected"},
            status=status.HTTP_403_FORBIDDEN,
        )

    data = request.data

    # Update host metadata from the payload
    host.last_checkin = now()
    host.last_relay_id = getattr(request, "vigil_relay_id", None)
    # IP is taken from the connection, never the agent-supplied body: a rogue
    # agent must not be able to steer Nessus scans or .rdp targets at hosts it
    # does not own. Matches register() above.
    if remote_addr := request.META.get("REMOTE_ADDR"):
        host.ip_address = remote_addr
    for field in ("hostname", "os", "kernel"):
        if val := data.get(field):
            setattr(host, field, val)
    if val := str(data.get("machine_id") or "").strip()[:200]:
        host.machine_id = val

    # Sync mode from agent config so the server always reflects what the agent
    # will accept. This is the design and stays: CLAUDE.md is explicit that the
    # agent's mode is authoritative, and 07b of the M6 audit confirmed nothing
    # the server sends can change it — so a host claiming full_control it does
    # not have receives tasks its own allowlist then refuses. It cannot
    # escalate; it can only misrepresent itself.
    #
    # But mode decides what an operator believes they can deploy where, so a
    # change to it is worth a line in the log. Silently accepting a host that
    # went from monitor to full_control is how an operator ends up targeting a
    # machine on the strength of something the machine said about itself.
    if agent_mode := data.get("mode"):
        valid_modes = {m.value for m in Host.Mode}
        if agent_mode in valid_modes:
            if agent_mode != host.mode:
                logger.info(
                    "host %s reported a mode change: %s -> %s",
                    host.hostname, host.mode, agent_mode)
            host.mode = agent_mode
        else:
            logger.warning(
                "host %s reported an unknown mode %r — keeping %s",
                host.hostname, agent_mode, host.mode)

    if agent_ver := data.get("vigil_version"):
        host.agent_version = str(agent_ver)[:50]

    # The next signing key this agent says it holds (sprint M3 phase 3). Only
    # a well-formed public key is stored; it decides nothing on its own, it
    # only tells `signing_key rotate` who is ready.
    ack = data.get("key_ack")
    if isinstance(ack, str) and len(ack) == 44 and ack.endswith("="):
        host.signing_key_ack = ack

    # The allowlist the agent will enforce. Anything malformed leaves the stored
    # value alone rather than wiping what a good check-in said.
    raw_allow = data.get("allowlist")
    if (isinstance(raw_allow, list) and len(raw_allow) <= 200
            and all(isinstance(a, str) and 0 < len(a) <= 60 for a in raw_allow)):
        host.agent_allowlist = sorted(set(raw_allow))
    if isinstance(data.get("allow_reprovision"), bool):
        host.agent_allow_reprovision = data["allow_reprovision"]
    # QA-56: the same local opt-in for live sessions. Written only here, by the
    # agent's own check-in — never by an admin pressing a button in the UI.
    # Booleans only, as allow_reprovision: the string "false" is not consent.
    if isinstance(data.get("allow_remote_shell"), bool):
        host.agent_allow_remote_shell = data["allow_remote_shell"]
    if isinstance(data.get("allow_remote_desktop"), bool):
        host.agent_allow_remote_desktop = data["allow_remote_desktop"]
    # Absent means an agent too old to report it (or Windows) — keep what we knew.
    if isinstance(data.get("runs_as_root"), bool):
        host.agent_runs_as_root = data["runs_as_root"]

    if data.get("features") is not None:
        # A list of short strings the agent understands (task-language
        # features). Anything else is treated as absent: a corrupt payload
        # must not silently grant features the agent never advertised.
        host.agent_features = _normalize_agent_features(data.get("features"))

    # reboot_required: an ABSENT key means the agent is too old to report
    # it — leave the stored value alone. An explicit False does write.
    if data.get("reboot_required") is not None:
        host.reboot_required = bool(data["reboot_required"])

    # Merge tags advertised by the agent.yml — namespaced under agent:*
    # so a rogue agent can't impersonate operator-set tags used for
    # deploy targeting. Server-side tags always win.
    if "tags" in data:
        host.tags = _normalize_tags(
            _namespace_agent_tags(data.get("tags")), existing=host.tags
        )

    # A previously offline host that successfully checks in is back online
    if host.status == Host.Status.OFFLINE:
        host.status = Host.Status.ONLINE
        from apps.alerts.models import Alert as _Alert
        _Alert.objects.filter(
            host=host,
            rule=None,
            state=_Alert.State.FIRING,
            message__startswith="Host offline:",
        ).update(state=_Alert.State.RESOLVED, resolved_at=now())

    # Auto-tag based on intrinsic facts (OS family, mode). Runs after the
    # agent.yml merge so derived tags appear alongside operator-set ones.
    host.tags = merge_auto_tags(host)

    host.save()

    # A rebuilt host's first check-in closes out its rebuild job: operator
    # tag applied, maintenance window cleared, playbook dispatched. No-op for
    # every other host. Never raises.
    from apps.reprovision.completion import complete_if_rebuilding

    complete_if_rebuilding(host)

    # Inventory upsert — agent sends the snapshot on a slower cadence than
    # metrics. Custom columns (populated by collector tasks) are preserved.
    inv_payload = data.get("inventory")
    if isinstance(inv_payload, dict):
        inv, _ = HostInventory.objects.get_or_create(host=host)
        inv.mac_addresses = inv_payload.get("mac_addresses") or {}
        try:
            inv.ram_total_bytes = int(inv_payload.get("ram_total_bytes") or 0) or None
        except (TypeError, ValueError):
            inv.ram_total_bytes = None
        inv.cpu_model = (inv_payload.get("cpu_model") or "")[:255]
        try:
            inv.cpu_cores = int(inv_payload.get("cpu_cores") or 0) or None
        except (TypeError, ValueError):
            inv.cpu_cores = None
        inv.service_tag = (inv_payload.get("service_tag") or "")[:120]
        inv.manufacturer = (inv_payload.get("manufacturer") or "")[:120]
        inv.model_name = (inv_payload.get("model") or inv_payload.get("model_name") or "")[:160]
        inv.os_name = (inv_payload.get("os_name") or "")[:200]
        inv.os_version = (inv_payload.get("os_version") or "")[:120]
        inv.kernel_version = (inv_payload.get("kernel_version") or "")[:120]
        inv.architecture = (inv_payload.get("architecture") or "")[:32]
        try:
            inv.uptime_seconds = int(inv_payload.get("uptime_seconds") or 0) or None
        except (TypeError, ValueError):
            inv.uptime_seconds = None
        inv.last_logged_user = (inv_payload.get("last_logged_user") or "")[:120]
        inv.bios_version = (inv_payload.get("bios_version") or "")[:120]
        inv.bios_date = (inv_payload.get("bios_date") or "")[:50]
        inv.system_timezone = (inv_payload.get("system_timezone") or "")[:80]
        disks = inv_payload.get("disks") or []
        inv.disks = disks if isinstance(disks, list) else []
        inv.save()

        # Auto-classified tags — sync os:/os_family:/pkg:/arch: values
        # into Host.tags so target_tags filters and conditional template
        # branches have something to match. User-set tags are preserved;
        # only the colon-prefixed namespace we manage gets refreshed.
        _sync_host_auto_tags(host, inv_payload)

    # Software list — replace-the-world per host, short-circuited by digest.
    # Never raises: a bad software list must not break a check-in.
    software_payload = data.get("software")
    if isinstance(software_payload, dict):
        from apps.software.ingest import ingest_software
        ingest_software(host, software_payload)

    # Absent means the agent cannot count (not Windows, too old, or the scan
    # failed), so the stored value is left alone rather than being zeroed by a
    # machine that does not know.
    win = data.get("windows_updates")
    if isinstance(win, dict):
        host.windows_updates = {
            key: int(win.get(key) or 0)
            for key in ("pending", "critical", "important", "reboot_required")
        }
        host.windows_updates_at = now()
        host.save(update_fields=["windows_updates", "windows_updates_at"])
    engines = data.get("container_engines")
    if isinstance(engines, list):
        clean = []
        for e in engines[:8]:
            if isinstance(e, dict) and e.get("kind") in ("docker", "podman"):
                clean.append({"kind": e["kind"], "version": str(e.get("version") or "")[:40],
                              "api_version": str(e.get("api_version") or "")[:10],
                              "rootless": bool(e.get("rootless")),
                              **({"uid": e["uid"]} if isinstance(e.get("uid"), int) else {})})
        host.container_engines = clean
        host.save(update_fields=["container_engines"])
    # The per-update list comes only after a fresh scan; absent leaves the
    # stored rows alone (an old agent, or the cached summary).
    if "windows_update_list" in data:
        from apps.software.updates import ingest_windows_updates
        ingest_windows_updates(host, data.get("windows_update_list"))

    # Docker container snapshot — replace the host's set wholesale. Absent key
    # means the agent didn't report (old agent / no docker) and we leave the
    # existing rows alone; an explicit empty list means "no containers now".
    containers_payload = data.get("docker_containers")
    if isinstance(containers_payload, list):
        rows = []
        for c in containers_payload:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")[:200]
            if not name:
                continue
            rows.append(DockerContainer(
                host=host,
                container_id=str(c.get("container_id") or c.get("id") or "")[:64],
                name=name,
                image=str(c.get("image") or "")[:255],
                state=str(c.get("state") or "")[:20],
                status=str(c.get("status") or "")[:120],
                stack=str(c.get("stack") or "")[:200],
                service=str(c.get("service") or "")[:200],
                cpu_percent=_safe_float(c.get("cpu_percent")),
                mem_usage_bytes=_safe_int(c.get("mem_usage_bytes")),
                mem_limit_bytes=_safe_int(c.get("mem_limit_bytes")),
                mem_percent=_safe_float(c.get("mem_percent")),
                ports=c.get("ports") if isinstance(c.get("ports"), list) else [],
                image_id=str(c.get("image_id") or "")[:80],
                image_digest=str(c.get("image_digest") or "")[:300],
                restart_policy=str(c.get("restart_policy") or "")[:40],
                config_hash=str(c.get("config_hash") or "")[:80],
                networks=_edge_list(c.get("networks"), 16),
                mounts=_edge_list(c.get("mounts"), 32),
            ))
        if len(rows) > MAX_CONTAINERS_PER_CHECKIN:
            logger.warning(
                "host %s reported %d containers; keeping the first %d",
                host.hostname, len(rows), MAX_CONTAINERS_PER_CHECKIN)
            rows = rows[:MAX_CONTAINERS_PER_CHECKIN]
        with transaction.atomic():
            DockerContainer.objects.filter(host=host).delete()
            if rows:
                DockerContainer.objects.bulk_create(rows)
            host.docker_snapshot_at = now()
            host.save(update_fields=["docker_snapshot_at"])
            _sync_container_stacks(host, containers_payload)

    if isinstance(data.get("docker_resources"), dict):
        _ingest_docker_resources(host, data["docker_resources"])

    if isinstance(data.get("network"), dict):
        _ingest_network(host, data["network"])

    # Pending hosts must wait for admin approval before receiving tasks
    if host.status == Host.Status.PENDING:
        return Response({"status": "pending", "tasks": []})

    # Ingest metrics
    raw_metrics = data.get("metrics", [])
    if raw_metrics:
        # A check-in body is whatever the machine sent, and a machine that is
        # compromised or simply broken can send as much of it as it likes.
        # Nothing bounded this: every entry became a row, so one host could
        # write the database full on its own. The agent reports roughly 65
        # metrics per check-in for an ordinary machine and a few hundred for a
        # large one, so this ceiling is far above any honest payload and far
        # below a useful attack.
        if len(raw_metrics) > MAX_METRICS_PER_CHECKIN:
            logger.warning(
                "host %s sent %d metrics in one check-in; keeping the first %d",
                host.hostname, len(raw_metrics), MAX_METRICS_PER_CHECKIN)
            raw_metrics = raw_metrics[:MAX_METRICS_PER_CHECKIN]

        points = []
        for m in raw_metrics:
            try:
                time_val = parse_datetime(m["time"]) if m.get("time") else now()
                points.append(
                    MetricPoint(
                        host=host,
                        time=time_val or now(),
                        # Truncated to the column widths rather than left to
                        # raise: the model declares max_length and an
                        # over-long value from an agent used to reach the
                        # database and fail the whole batch.
                        category=str(m["category"])[:50],
                        metric=str(m["metric"])[:100],
                        value=float(m["value"]),
                        labels=_bounded_labels(m.get("labels")),
                    )
                )
            except (KeyError, ValueError, TypeError):
                pass  # Skip malformed metric entries silently
        if points:
            MetricPoint.objects.bulk_create(points)

    # Sign and hand off any pending tasks. Two gates apply before dispatch:
    #   1. ``not_before`` — used to space out retries by retry_delay_seconds
    #   2. ``schedule.window`` — agents only receive tasks during the
    #      configured maintenance window. Tasks outside the window remain
    #      PENDING and are picked up at the next eligible checkin.
    from apps.instance.config import setting as instance_setting
    current = now()
    _tz_name = instance_setting("VIGIL_TIMEZONE")
    try:
        _tz = zoneinfo.ZoneInfo(_tz_name)
        _local = current.astimezone(_tz)
    except Exception:
        _local = current
    eligible: list[Task] = []
    candidates = list(Task.objects.filter(host=host, state=Task.State.PENDING))
    if candidates:
        # Safety gate before the window check: a deferral-bearing reboot must
        # never reach an agent too old to honour it. Refused tasks fail now;
        # everything else follows the normal not_before / window path.
        _refuse_outdated_reboot(host)
        # The feature gate: a task whose params carry a `relevant:` block must
        # never reach an agent that cannot understand it (it would ignore the
        # block and run the fix on every host). Same shape as the reboot gate.
        _refuse_unsupported_features(host)
        # Re-query: the gates saved state on their own querysets, and the
        # in-memory candidates above are stale copies the bulk DISPATCHED
        # update below would resurrect.
        candidates = list(Task.objects.filter(host=host, state=Task.State.PENDING))
        weekday = _local.weekday()
        hour = _local.hour
        minute = _local.minute
        for task in candidates:
            if task.expires_at is not None and task.expires_at <= current:
                # The hunt's stays_open has elapsed; the sweep expires it.
                continue
            if task.not_before and task.not_before > current:
                continue
            if not schedule_window_active(task.schedule, weekday=weekday, hour=hour, minute=minute):
                continue
            eligible.append(task)

    tasks_payload = []
    if eligible:
        # Snapshot a single dispatch timestamp so every task in this checkin
        # response gets the same reference time and the DB write matches what
        # the agent receives.
        dispatch_ts = now()
        dispatch_iso = dispatch_ts.isoformat()
        # An agent that understands v2 gets a signature over the dispatch time
        # and its own identity too (SEC-4), and refuses anything less.
        signed_v2 = "signed_v2" in set(host.agent_features or [])
        for task in eligible:
            if signed_v2:
                signature = sign_task_v2(task, dispatch_iso, host.token_fingerprint)
            else:
                if not task.signature:
                    task.signature = sign_task(task)
                    task.save(update_fields=["signature"])
                signature = task.signature
            tasks_payload.append(
                {
                    "id": str(task.id),
                    "host_id": str(task.host_id),
                    "action": task.action,
                    "params": task.params,
                    "nonce": task.nonce,
                    "signature": signature,
                    **({"sig_v": 2} if signed_v2 else {}),
                    "ttl_seconds": task.ttl_seconds,
                    # ``dispatched_at`` is what the agent uses for TTL — it
                    # represents when the signed payload went on the wire, so
                    # a task that waited days for a schedule.window can still
                    # be processed. ``created_at`` is kept for older agents
                    # but should not be used as a freshness proof.
                    "dispatched_at": dispatch_iso,
                    "created_at": task.created_at.isoformat(),
                }
            )
        Task.objects.filter(id__in=[t.id for t in eligible]).update(
            state=Task.State.DISPATCHED, dispatched_at=dispatch_ts
        )

    from apps.software.models import SoftwareSnapshot

    from apps.relays.models import signed_relay_list

    snapshot = SoftwareSnapshot.objects.filter(host=host).first()
    return Response(
        {
            "status": "ok",
            "public_key": get_public_key_b64(),
            # While a rotation is under way: the next key, signed by the
            # active one, which the agent checks before it holds the key.
            **({"key_rotation": statement} if (statement := rotation_statement()) else {}),
            "tasks": tasks_payload,
            # Fallback connection points (QA-52), signed for this agent (QA-54b)
            # because a relay carries this response too: "relays" is the ordered
            # list, "direct" whether the dashboard itself still accepts agents,
            # and "relays_sig" what proves neither was written by the relay.
            **signed_relay_list(host.token_fingerprint),
            # The agent resends its software list when this differs from its
            # own digest. "" when the server holds nothing for this host.
            "software_digest": snapshot.digest if snapshot else "",
        }
    )


@extend_schema(methods=["GET"], responses=HostsTransportAckSerializer)
@extend_schema(methods=["POST"], request=HostsTransportAckBodySerializer,
               responses=HostsTransportAckSerializer)
@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def transport_ack(request):
    """Read or set the standing acknowledgement that this instance runs on
    plain HTTP.

    Admin-only, and it records who: this is a deliberate downgrade of the
    instance's security posture, not a preference. GET is safe for any admin
    to call; POST with {"acknowledged": true|false} sets or withdraws it.
    """
    ack = TransportAck.load()

    if request.method == "POST":
        wanted = bool(request.data.get("acknowledged"))
        ack.acknowledged = wanted
        # Cleared rather than kept on withdrawal — a stale name beside a
        # withdrawn acknowledgement reads as though it still stands.
        ack.acknowledged_by = request.user if wanted else None
        ack.acknowledged_at = now() if wanted else None
        ack.save()

    return Response({
        "acknowledged": ack.acknowledged,
        "acknowledged_by": ack.acknowledged_by.username if ack.acknowledged_by else "",
        "acknowledged_at": ack.acknowledged_at,
        # What the browser is talking over right now, so the pane can say
        # whether the acknowledgement is presently doing anything.
        "request_is_secure": request.is_secure(),
    })


@extend_schema(request=None, responses={200: HostsHostSerializer, 404: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def host_poll(request, host_id):
    """Request an immediate check-in.

    Since agents are outbound-only, the server cannot push to them. This
    returns the current host status; the agent will pick up any queued tasks
    on its next scheduled check-in.
    """
    host, denied = scoping.host_or_404(request, host_id)
    if denied:
        return denied
    return Response(HostSerializer(host).data)


@extend_schema(request=HostsCheckPendingTokenSerializer, responses={
    200: HostsCheckPendingSerializer, 400: HostsErrorSerializer,
    403: HostsErrorSerializer})
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def check_pending(request):
    """Enrollment wizard: check whether a host with the given token has appeared.

    Returns:
      {"status": "waiting"}              — no host with that token yet
      {"status": "pending", "host": {…}} — host registered, awaiting approval
      {"status": "approved", "host": {…}} — host already approved
    """
    # Enrolment is an admin flow, and the answer describes a host (SEC-1).
    from apps.accounts.permissions import IsAdmin
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Administrator access required."}, status=status.HTTP_403_FORBIDDEN)
    token = (request.data.get("token") or "").strip()
    if not token:
        return Response({"error": "token is required"}, status=status.HTTP_400_BAD_REQUEST)

    host = Host.objects.by_token(token).first()
    if host is None:
        return Response({"status": "waiting"})

    # The wizard's "replaces" hint must be honest in every branch: for a pending
    # host that is the host being approved, for one already approved it is the
    # record it would have moved onto.
    candidate = replacement_candidate(host)

    host_data = HostSerializer(host).data
    replaces = (
        None
        if candidate is None
        else {
            "id": str(candidate.id),
            "hostname": candidate.hostname,
            "last_checkin": candidate.last_checkin,
        }
    )
    if host.status == Host.Status.PENDING:
        return Response({"status": "pending", "host": host_data, "replaces": replaces})
    return Response({"status": "approved", "host": host_data, "replaces": replaces})
