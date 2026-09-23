"""Replacing a host record when a machine re-enrols.

A host's identity used to be its agent token: the token is handed out fresh
on every install, so reinstalling the agent on a machine we already have made
a second host record, and the fleet showed the device twice. Phase 02 gave
every host a ``machine_id`` — a stable per-machine fingerprint — so an
approval can now move the machine onto its existing record instead.
"""

from django.db import transaction

from .models import Host


def replacement_candidate(pending):
    """The already-approved record for the same physical machine, or None.

    Matches on machine_id only: it is never empty-matched (a host whose platform
    gave no fingerprint must not collide with every other such host), never the
    pending row itself, and only against a record an admin already approved
    (online/offline) — a second *pending* row is a duplicate to clear, not a
    record to adopt.
    """
    if not pending.machine_id:
        return None
    return (
        Host.objects.filter(
            machine_id=pending.machine_id,
            status__in=[Host.Status.ONLINE, Host.Status.OFFLINE],
        )
        .exclude(pk=pending.pk)
        .order_by("-last_checkin")
        .first()
    )


def adopt_enrolment(pending, existing):
    """Move the machine's new credentials onto `existing` and drop `pending`.

    The surviving row keeps its id, tags, site assignment and metric history, so
    every dashboard, task and alert that already points at this machine follows
    it. Returns the surviving host.
    """
    # Local imports: apps.metrics and apps.hosts.models are both fine at module
    # level, but keeping them here makes this module importable without Django
    # app registry order assumptions.
    from apps.metrics.models import MetricPoint

    from .models import HostInventory

    with transaction.atomic():
        # Snapshot the live identity and the pending row's pk before the row
        # goes away; the cascade would otherwise drop its history with it.
        pending_pk = pending.pk
        existing_pk = existing.pk
        incoming = {
            "agent_token": pending.agent_token,
            "hostname": pending.hostname,
            "os": pending.os,
            "kernel": pending.kernel,
            "ip_address": pending.ip_address,
            "machine_id": pending.machine_id,
            "agent_version": pending.agent_version,
            "last_checkin": pending.last_checkin,
            "mode": pending.mode,
        }

        # Reassign history onto the surviving record before the pending row
        # goes away: MetricPoint is a CASCADE FK, so the delete would drop it.
        MetricPoint.objects.filter(host_id=pending_pk).update(host_id=existing_pk)

        if not HostInventory.objects.filter(host_id=existing_pk).exists():
            HostInventory.objects.filter(host_id=pending_pk).update(host_id=existing_pk)

        # Drop the incoming row first so its agent_token stops being
        # unique-constrained before the surviving row takes over the machine's
        # new credentials.
        pending.delete()

        existing.agent_token = incoming["agent_token"]
        existing.hostname = incoming["hostname"]
        existing.os = incoming["os"]
        existing.kernel = incoming["kernel"]
        existing.ip_address = incoming["ip_address"]
        existing.machine_id = incoming["machine_id"]
        existing.agent_version = incoming["agent_version"]
        existing.last_checkin = incoming["last_checkin"]
        existing.mode = incoming["mode"]
        existing.status = Host.Status.ONLINE
        existing.save()

    return existing
