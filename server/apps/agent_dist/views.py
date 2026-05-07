import hashlib
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpResponse
from django.template.loader import render_to_string
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import AgentBinary


def _dist_dir() -> Path:
    return Path(getattr(settings, "VIGIL_AGENT_DIST_DIR", settings.BASE_DIR / "agent_dist"))


def _bundled_path(platform: str) -> Path:
    return _dist_dir() / f"vigil-agent-{platform}"


@api_view(["GET"])
@permission_classes([AllowAny])
def download_agent(request, platform):
    """Serve the agent binary for the requested platform.

    Checks the filesystem bundle first (Docker build artifact), then falls
    back to a manually-uploaded DB record. Public — agents download before
    they have auth tokens.
    """
    bundled = _bundled_path(platform)
    if bundled.is_file():
        response = FileResponse(
            open(bundled, "rb"),
            content_type="application/octet-stream",
        )
        filename = f"vigil-agent-{platform}"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Vigil-Version"] = getattr(settings, "VIGIL_AGENT_VERSION", "")
        return response

    # Fall back to DB record (manually uploaded override)
    try:
        record = AgentBinary.objects.get(platform=platform)
    except AgentBinary.DoesNotExist:
        return Response({"error": f"No agent binary for platform '{platform}'"}, status=404)

    response = FileResponse(
        record.binary.open("rb"),
        content_type="application/octet-stream",
    )
    filename = f"vigil-agent-{platform}"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    if record.version:
        response["X-Vigil-Version"] = record.version
    if record.sha256:
        response["X-Vigil-SHA256"] = record.sha256
    return response


def install_script(request):
    """Return a shell script that downloads and installs the agent."""
    base_url = f"{request.scheme}://{request.get_host()}"
    content = render_to_string("agent_install.sh", {"base_url": base_url})
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser])
def upload_agent(request, platform):
    """Upload a new agent binary for the given platform (manual override)."""
    valid_platforms = dict(AgentBinary.Platform.choices)
    if platform not in valid_platforms:
        return Response({"error": f"Unknown platform '{platform}'"}, status=400)

    file_obj = request.FILES.get("binary")
    if not file_obj:
        return Response({"error": "No binary file in request"}, status=400)

    version = (request.data.get("version") or "").strip()

    sha256 = hashlib.sha256()
    for chunk in file_obj.chunks():
        sha256.update(chunk)
    file_obj.seek(0)
    digest = sha256.hexdigest()

    record, _ = AgentBinary.objects.get_or_create(platform=platform)
    if record.binary:
        try:
            record.binary.delete(save=False)
        except Exception:
            pass
    record.version = version
    record.sha256 = digest
    record.binary = file_obj
    record.save()

    return Response({"platform": platform, "version": version, "sha256": digest})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def agent_info(request):
    """Return metadata for all available agent binaries.

    Prefers the bundled filesystem binary; falls back to DB records.
    """
    bundled_version = getattr(settings, "VIGIL_AGENT_VERSION", "")
    platforms = [c[0] for c in AgentBinary.Platform.choices]
    platform_labels = dict(AgentBinary.Platform.choices)
    results = []

    for platform in platforms:
        bundled = _bundled_path(platform)
        if bundled.is_file():
            results.append({
                "platform": platform,
                "platform_label": platform_labels[platform],
                "version": bundled_version,
                "sha256": "",
                "source": "bundled",
            })
        else:
            try:
                r = AgentBinary.objects.get(platform=platform)
                results.append({
                    "platform": r.platform,
                    "platform_label": r.get_platform_display(),
                    "version": r.version,
                    "sha256": r.sha256,
                    "source": "uploaded",
                })
            except AgentBinary.DoesNotExist:
                pass

    return Response(results)
