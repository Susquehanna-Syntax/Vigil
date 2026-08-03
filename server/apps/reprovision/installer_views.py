"""The only unauthenticated surface in this app.

The installer holds no credentials, so an unguessable URL is the only gate
available. Everything here exists to make that gate narrow: a short window,
an IP pin, one-time redemption, and nothing worth stealing in the payload.
Do not edit without re-reading docs/reprovisioning.md §4.2 and §4.3.
"""
import logging
import os
from pathlib import Path

from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import (api_view, permission_classes,
                                       throttle_classes)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from apps.hosts.models import Host
from vigil import hooks

from . import jobs
from .models import OSImage, RebuildJob
from .renderers import get_renderer

logger = logging.getLogger("vigil.reprovision")


class _EnrolThrottle(AnonRateThrottle):
    """Per-IP cap, mirroring the throttle on hosts.register()."""

    scope = "reprovision_enroll"
    rate = "30/hour"


def _base_url(request) -> str:
    return f"{request.scheme}://{request.get_host()}"


def _client_ip(request) -> str | None:
    return request.META.get("REMOTE_ADDR")


@csrf_exempt
def answer_file(request, token: str, filename: str = ""):
    """Serve the rendered answer file.

    Servable only while the job is REBOOTING or INSTALLING. The first fetch
    is the only positive proof the installer booted — and the moment the disk
    starts being wiped, which is why the old agent token is revoked here.
    """
    job = RebuildJob.objects.filter(
        answer_token_hash=jobs.hash_token(token)).select_related(
            "host", "image", "profile").first()
    if job is None or job.state not in RebuildJob.ANSWER_SERVABLE_STATES:
        raise Http404

    ip = _client_ip(request)
    if job.answer_fetch_ip and job.answer_fetch_ip != ip:
        # Repeat fetches are legitimate (subiquity re-reads its data source),
        # but only from the machine that made the first one.
        logger.warning("rebuild %s: answer fetch from %s, pinned to %s",
                       job.id, ip, job.answer_fetch_ip)
        raise Http404

    enroll_plain = jobs.pop_enroll_token(job)
    if not enroll_plain:
        # Rendering without it would produce a machine that installs fine and
        # can never enrol. Fail loudly instead.
        logger.error("rebuild %s: enrolment token missing at render time", job.id)
        raise Http404

    rendered = get_renderer(job.image.os_family).render(
        job, _base_url(request), enroll_plain)

    key = filename or next(iter(rendered))
    if key not in rendered:
        raise Http404

    if job.answer_fetched_at is None:
        job.answer_fetched_at = now()
        job.answer_fetch_ip = ip
        job.save(update_fields=["answer_fetched_at", "answer_fetch_ip"])
        _revoke_old_agent_token(job)
        jobs.advance(job, RebuildJob.State.INSTALLING)

    hooks.emit("rebuild_answer_fetched", job=job, ip=ip,
               user_agent=request.META.get("HTTP_USER_AGENT", ""))

    response = HttpResponse(rendered[key], content_type="text/plain")
    response["Cache-Control"] = "no-store"
    return response


def _revoke_old_agent_token(job) -> None:
    """Invalidate the pre-rebuild token at the point of no return (§4.3).

    Without this, a token recovered from a backup or forensic image of the
    wiped disk authenticates as this host forever.
    """
    host = job.host
    host.agent_token = f"revoked-{job.id}"
    host.save(update_fields=["agent_token"])


@csrf_exempt
def install_tree(request, image_id, path: str = ""):
    """Serve the extracted ISO tree as the installer's package source."""
    image = OSImage.objects.filter(pk=image_id,
                                   status=OSImage.Status.READY).first()
    if image is None or not image.tree_path:
        raise Http404

    root = Path(image.tree_path).resolve()
    # The URL is attacker-controllable; normalise before comparing so that
    # ../ cannot walk out of the tree into the rest of the image directory.
    target = Path(os.path.normpath(root / path.lstrip("/")))
    if target != root and root not in target.parents:
        raise Http404
    if not target.is_file():
        raise Http404
    return FileResponse(target.open("rb"))


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([_EnrolThrottle])
def enroll(request):
    """Redeem a one-time enrolment token, rotating the host's agent token."""
    token = (request.data.get("enroll_token") or "").strip()
    agent_token = (request.data.get("agent_token") or "").strip()
    if not token or not agent_token:
        return Response({"error": "enroll_token and agent_token are required"},
                        status=400)

    token_hash = jobs.hash_token(token)
    with transaction.atomic():
        # Single conditional UPDATE, not read-then-write: two concurrent
        # redemptions must not both win (§4.3). The rowcount is the lock.
        claimed = RebuildJob.objects.filter(
            enroll_token_hash=token_hash,
            enroll_consumed_at__isnull=True,
            state__in=[RebuildJob.State.INSTALLING, RebuildJob.State.ENROLLING],
        ).update(enroll_consumed_at=now())
        if not claimed:
            return Response({"error": "Invalid or already-used token"},
                            status=403)

        job = RebuildJob.objects.select_related("host").get(
            enroll_token_hash=token_hash)
        host = job.host
        host.agent_token = agent_token
        # Approved hosts are ONLINE; there is no separate APPROVED status.
        # hosts.views.host_approve sets exactly this.
        host.status = Host.Status.ONLINE
        host.os = ""
        host.kernel = ""
        host.agent_version = ""
        host.save(update_fields=["agent_token", "status", "os", "kernel",
                                 "agent_version"])
        _clear_stale_os_state(host)
        if job.state == RebuildJob.State.INSTALLING:
            jobs.advance(job, RebuildJob.State.ENROLLING)

    return Response({"id": str(host.id), "status": host.status}, status=200)


def _clear_stale_os_state(host) -> None:
    """Drop records describing an operating system that no longer exists.

    Leaving them means the dashboard reports packages and CVEs for a dead
    install — a correctness bug that reads as a security bug.
    """
    from apps.hosts.models import DockerContainer, HostInventory
    from apps.vulns.models import VulnFinding

    HostInventory.objects.filter(host=host).delete()
    DockerContainer.objects.filter(host=host).delete()
    VulnFinding.objects.filter(host=host).delete()
