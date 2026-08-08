"""Authenticated reprovisioning API.

Objects outside the caller's sites answer 404, not 403 — the convention set
in 2026.5.0, so an id probe cannot confirm a resource exists.
"""
import logging
import shutil
from datetime import timedelta

from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin, can
from vigil import hooks, scoping

from . import jobs
from .catalog import CATALOG, get_entry
from .ceremony import verify_rebuild_confirmation
from .fetch_guard import check_url
from .images import image_root
from .models import InstallProfile, OSImage, RebuildJob
from .serializers import (InstallProfileSerializer, OSImageSerializer,
                          RebuildJobSerializer)
from .tasks import fetch_image

logger = logging.getLogger("vigil.reprovision")


def _may(user, host, verb: str) -> bool:
    """Capability check in the host's site, degrading to the unscoped role
    when the Business sites app is absent."""
    site = scoping.scope_of(host) if host is not None else None
    return can(user, site, "reprovision", verb)


# ── Image catalog (admin only — see §4.4) ────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def catalog_list(request):
    """The distros Vigil knows how to fetch on its own. Static data, so
    there is nothing here an operator can break by reading it — same `view`
    gate as the image library itself."""
    if not can(request.user, None, "reprovision", "view"):
        return Response({"error": "Not found"}, status=404)
    return Response(CATALOG)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def image_pull(request):
    """Register an image and queue its download.

    Either a `catalog_id` (URL and digest come from the catalog — never from
    the request body, so a caller-supplied sha256 alongside a catalog_id is
    ignored) or a custom `url` + `sha256` + `os_family` + `name`.

    `check_url` runs before any row is created: a refused destination must
    leave no `OSImage` behind, or the library fills with rows for fetches
    Vigil never attempted.
    """
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Admin required"}, status=403)

    catalog_id = request.data.get("catalog_id")
    if catalog_id:
        entry = get_entry(catalog_id)
        if entry is None:
            return Response(
                {"error": f"Unknown catalog id {catalog_id!r}"}, status=400)
        url = entry["url"]
        sha256 = entry["sha256"]
        os_family = entry["os_family"]
        name = entry["name"]
        version = entry.get("version", "")
        architecture = entry.get("architecture", "x86_64")
        size_bytes = entry.get("size_bytes", 0)
    else:
        url = (request.data.get("url") or "").strip()
        sha256 = (request.data.get("sha256") or "").strip()
        os_family = request.data.get("os_family") or ""
        name = (request.data.get("name") or "").strip()
        version = request.data.get("version", "")
        architecture = request.data.get("architecture") or "x86_64"
        size_bytes = 0

        if not url:
            return Response({"error": "url is required"}, status=400)
        if not sha256:
            return Response(
                {"error": "A custom URL requires a sha256 digest — Vigil "
                          "cannot verify what it downloads without one."},
                status=400)
        if os_family not in OSImage.Family.values:
            return Response(
                {"error": f"Unknown os_family {os_family!r}; must be one "
                          f"of {', '.join(OSImage.Family.values)}"},
                status=400)
        if not name:
            return Response({"error": "name is required"}, status=400)

    if refusal := check_url(url):
        return Response({"error": refusal}, status=400)

    image = OSImage.objects.create(
        name=name, os_family=os_family, version=version,
        architecture=architecture, sha256=sha256, size_bytes=size_bytes,
        status=OSImage.Status.DOWNLOADING, source_url=url,
        created_by=request.user,
    )
    fetch_image.delay(image.id)
    return Response(OSImageSerializer(image).data, status=202)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def image_list(request):
    if request.method == "GET":
        if not can(request.user, None, "reprovision", "view"):
            return Response({"error": "Not found"}, status=404)
        return Response(OSImageSerializer(
            OSImage.objects.all(), many=True).data)

    # Whoever can register an image chooses what runs as root on every
    # rebuilt machine. Admin-only, and never delegated through the operator
    # matrix — same reasoning as agent_dist.upload_agent.
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Admin required"}, status=403)
    serializer = OSImageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    image = serializer.save(created_by=request.user)
    return Response(OSImageSerializer(image).data, status=201)


@api_view(["GET", "DELETE"])
@permission_classes([IsAuthenticated])
def image_detail(request, image_id):
    image = get_object_or_404(OSImage, pk=image_id)
    if request.method == "GET":
        if not can(request.user, None, "reprovision", "view"):
            return Response({"error": "Not found"}, status=404)
        return Response(OSImageSerializer(image).data)
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Admin required"}, status=403)
    tree_dir = image_root() / str(image.id)
    try:
        image.delete()
    except ProtectedError as exc:
        job = next(iter(exc.protected_objects), None)
        if isinstance(job, RebuildJob):
            detail = f"rebuild job {job.id} for host {job.host.hostname}"
        else:
            detail = "a rebuild job"
        return Response({
            "error": f"Image is still referenced by {detail} and cannot "
                     f"be deleted.",
        }, status=409)
    shutil.rmtree(tree_dir, ignore_errors=True)
    return Response(status=204)


# ── Install profiles ─────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def profile_list(request):
    if request.method == "GET":
        if not can(request.user, None, "reprovision", "view"):
            return Response({"error": "Not found"}, status=404)
        return Response(InstallProfileSerializer(
            InstallProfile.objects.all(), many=True).data)

    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Admin required"}, status=403)
    serializer = InstallProfileSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    profile = serializer.save(created_by=request.user)
    return Response(InstallProfileSerializer(profile).data, status=201)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def profile_detail(request, profile_id):
    profile = get_object_or_404(InstallProfile, pk=profile_id)
    if request.method == "GET":
        if not can(request.user, None, "reprovision", "view"):
            return Response({"error": "Not found"}, status=404)
        return Response(InstallProfileSerializer(profile).data)
    if not IsAdmin().has_permission(request, None):
        return Response({"error": "Admin required"}, status=403)
    if request.method == "DELETE":
        profile.delete()
        return Response(status=204)
    serializer = InstallProfileSerializer(profile, data=request.data,
                                          partial=True)
    serializer.is_valid(raise_exception=True)
    return Response(InstallProfileSerializer(serializer.save()).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def profile_preview(request, profile_id):
    """Render the answer file for a host, with the enrolment token redacted.

    Lets an operator see exactly what the installer will read before they
    commit to wiping anything.
    """
    from apps.hosts.models import Host

    from .renderers import get_renderer

    profile = get_object_or_404(InstallProfile, pk=profile_id)
    host = get_object_or_404(Host, pk=request.data.get("host"))
    if not _may(request.user, host, "view"):
        return Response({"error": "Not found"}, status=404)

    preview_job = RebuildJob(host=host, image=profile.image, profile=profile,
                             deadline=now() + timedelta(minutes=60))
    rendered = get_renderer(profile.image.os_family).render(
        preview_job, request.build_absolute_uri("/").rstrip("/"),
        "<enrolment-token-redacted>")
    return Response({"files": rendered})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def preflight_check(request):
    """Dispatch a readiness probe, and return the most recent result.

    Read-only on the agent side, so it needs only the `view` capability. The
    probe is a normal signed task, which means the answer arrives on the
    agent's next check-in rather than in this response — the caller gets the
    previous result immediately and a fresh one shortly after.
    """
    import secrets

    from apps.hosts.models import Host
    from apps.tasks.models import Task

    host = get_object_or_404(Host, pk=request.data.get("host"))
    if not _may(request.user, host, "view"):
        return Response({"error": "Not found"}, status=404)

    params = {}
    if disk := request.data.get("disk_target"):
        params["disk_target"] = disk
    if family := request.data.get("os_family"):
        params["os_family"] = family

    Task.objects.create(
        host=host, requested_by=request.user,
        step_label="rebuild readiness check",
        action="reprovision_preflight", params=params,
        risk_level=Task.RiskLevel.LOW, state=Task.State.PENDING,
        nonce=secrets.token_hex(32),
    )

    last = Task.objects.filter(
        host=host, action="reprovision_preflight",
        state=Task.State.COMPLETED).order_by("-completed_at").first()
    if last and last.result_output:
        import json
        try:
            return Response(json.loads(last.result_output))
        except ValueError:
            logger.warning("host %s: unparseable preflight output", host.id)
    return Response({"dispatched": True}, status=202)


# ── Rebuild jobs ─────────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def job_list(request):
    if request.method == "GET":
        qs = scoping.filter_by_site(
            RebuildJob.objects.select_related("host", "image", "profile"),
            request.user, path="host__")
        return Response(RebuildJobSerializer(qs, many=True).data)
    return _create_job(request)


def _create_job(request):
    from apps.hosts.models import Host

    host = get_object_or_404(Host, pk=request.data.get("host"))
    if not _may(request.user, host, "view"):
        return Response({"error": "Not found"}, status=404)
    if not _may(request.user, host, "rebuild"):
        return Response({"error": "Not permitted"}, status=403)

    image = get_object_or_404(OSImage, pk=request.data.get("image"))
    profile = get_object_or_404(InstallProfile, pk=request.data.get("profile"))

    if image.status != OSImage.Status.READY:
        return Response(
            {"error": f"Image is {image.status}, not ready to install"},
            status=400)
    if profile.image_id != image.id:
        return Response({"error": "Profile does not belong to that image"},
                        status=400)

    tag = (request.data.get("completion_tag") or "").strip()
    if tag.startswith("agent:"):
        # Reserved for agent-advertised tags so a rogue agent cannot
        # impersonate operator-set ones.
        return Response(
            {"error": "The agent: tag namespace is reserved"}, status=400)

    if not request.is_secure() and not request.data.get(
            "acknowledge_plaintext_transport"):
        return Response({
            "error": "Vigil is being served over plain HTTP. The answer file "
                     "carries the admin password hash, SSH keys, and the "
                     "enrolment token in clear text. Re-send with "
                     "acknowledge_plaintext_transport to proceed anyway.",
            "code": "plaintext_transport",
        }, status=400)

    if err := verify_rebuild_confirmation(request.user, request.data, host):
        return Response({"error": err}, status=401)

    baseline = None
    if baseline_id := request.data.get("post_baseline"):
        from apps.baselines.models import Baseline
        baseline = get_object_or_404(Baseline, pk=baseline_id)

    job = RebuildJob.objects.create(
        host=host, image=image, profile=profile, requested_by=request.user,
        confirmed_ip=request.META.get("REMOTE_ADDR"),
        completion_tag=tag, post_baseline=baseline,
        deadline=now() + timedelta(minutes=profile.deadline_minutes),
    )
    _answer, enroll = jobs.mint_tokens(job)
    jobs.stash_enroll_token(job, enroll)
    hooks.emit("rebuild_requested", job=job, actor=request.user,
               ip=job.confirmed_ip)
    logger.info("rebuild %s requested for %s by %s", job.id, host.hostname,
                request.user)
    return Response(RebuildJobSerializer(job).data, status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def job_detail(request, job_id):
    job = get_object_or_404(RebuildJob, pk=job_id)
    if not _may(request.user, job.host, "view"):
        return Response({"error": "Not found"}, status=404)
    return Response(RebuildJobSerializer(job).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def job_abort(request, job_id):
    job = get_object_or_404(RebuildJob, pk=job_id)
    if not _may(request.user, job.host, "view"):
        return Response({"error": "Not found"}, status=404)
    if not _may(request.user, job.host, "rebuild"):
        return Response({"error": "Not permitted"}, status=403)
    try:
        jobs.advance(job, RebuildJob.State.ABORTED,
                     reason=f"Aborted by {request.user}")
    except jobs.InvalidTransition:
        return Response(
            {"error": f"A job in state '{job.state}' can no longer be aborted"},
            status=409)
    return Response(RebuildJobSerializer(job).data)
