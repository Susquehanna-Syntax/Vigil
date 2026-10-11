"""The Store's API (QA-26): browse, one package, its icon, and turning the
catalog on."""
import hashlib
import threading
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Min, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.instance import config
from vigil import scoping

from .download import MAX_ICON_BYTES, FetchError, fetch
from .models import CatalogPackage, CatalogSync

PAGE = 48
_PNG = b"\x89PNG\r\n\x1a\n"
#: Each source's label in the Store, and the platform tab it belongs to.
SOURCE_LABELS = {"winget": "winget", "flatpak": "Flathub", "dpkg": "apt"}


def _card(p: CatalogPackage, installed: int) -> dict:
    return {"id": p.id, "source": p.source, "source_label": SOURCE_LABELS.get(p.source, p.source),
            "release": p.release, "package_id": p.package_id, "name": p.name, "summary": p.summary,
            "publisher": p.publisher, "version": p.version, "platform": p.platform,
            "categories": p.categories, "icon": bool(p.icon_url), "installed": installed}


def _installed_counts(user, pairs) -> dict:
    """{(source, package_id): hosts that have it}, scoped like the Apps page."""
    from apps.software.models import SoftwareItem

    if not pairs:
        return {}
    match = Q()
    for source, pid in pairs:
        match |= Q(source=source, package_id=pid)
    rows = (scoping.filter_by_site(SoftwareItem.objects.exclude(host__status="rejected"), user,
                                   path="host__")
            .filter(match).values("source", "package_id").annotate(n=Count("host", distinct=True)))
    return {(r["source"], r["package_id"]): r["n"] for r in rows}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def browse(request):
    """GET ?q=&platform=&source=&category=&page= — one card per package; an apt
    package offered by several releases is one card."""
    qs = CatalogPackage.objects.all()
    if platform := request.query_params.get("platform"):
        qs = qs.filter(platform=platform)
    if source := request.query_params.get("source"):
        qs = qs.filter(source=source)
    if category := request.query_params.get("category"):
        qs = qs.filter(categories__icontains=f'"{category}"')
    q = request.query_params.get("q", "").strip()[:100]
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(package_id__icontains=q) | Q(summary__icontains=q)
                       | Q(publisher__icontains=q))
    # one row per (source, package_id): the lowest id stands for the others
    firsts = qs.values("source", "package_id").annotate(first=Min("id")).values("first")
    qs = CatalogPackage.objects.filter(id__in=firsts)
    if q:
        # exact and prefix matches first, then apps with icons (Flathub's are the store's faces)
        qs = qs.order_by("name")
        rows = list(qs[:500])
        ql = q.lower()
        rows.sort(key=lambda p: (p.name.lower() != ql,
                                 not p.name.lower().startswith(ql),
                                 f" {ql}" not in f" {p.name.lower()}",   # "Mozilla Firefox" for "firefox"
                                 not p.icon_url, p.name.lower()))
        total = len(rows)
    else:
        total = qs.count()
        rows = None
        qs = qs.order_by("-icon_url", "name")
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    start = (page - 1) * PAGE
    window = rows[start:start + PAGE] if rows is not None else list(qs[start:start + PAGE])
    counts = _installed_counts(request.user, {(p.source, p.package_id) for p in window})
    enabled = bool(config.setting("VIGIL_CATALOG_SYNC"))
    return Response({
        "count": total, "page": page, "page_size": PAGE,
        "results": [_card(p, counts.get((p.source, p.package_id), 0)) for p in window],
        "enabled": enabled, "empty": not CatalogPackage.objects.exists(),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def categories(request):
    """The most common categories, for the chips (from a sample, not every row)."""
    tally: dict = {}
    platform = request.query_params.get("platform")
    qs = CatalogPackage.objects.exclude(categories=[])
    if platform:
        qs = qs.filter(platform=platform)
    # apt sections are not categories; winget's tags only make sense on the
    # Windows tab, where they are all there is.
    skip = ["dpkg"] if platform == "windows" else ["dpkg", "winget"]
    for cats in qs.exclude(source__in=skip).values_list("categories", flat=True)[:20000]:
        for c in cats or []:
            tally[c] = tally.get(c, 0) + 1
    top = sorted(tally.items(), key=lambda kv: -kv[1])[:16]
    return Response({"results": [{"name": k, "count": v} for k, v in top]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def detail(request, package_id):
    p = get_object_or_404(CatalogPackage, pk=package_id)
    releases = list(CatalogPackage.objects.filter(source=p.source, package_id=p.package_id)
                    .order_by("release").values("release", "version"))
    # The same app from other package managers, by exact name.
    # "Firefox" by Mozilla is winget's "Mozilla Firefox": try the name with
    # and without the publisher in front.
    names = Q(name__iexact=p.name)
    if p.publisher:
        names |= Q(name__iexact=f"{p.publisher} {p.name}")
        if p.name.lower().startswith(p.publisher.lower() + " "):
            names |= Q(name__iexact=p.name[len(p.publisher) + 1:])
    others = [o for o in CatalogPackage.objects.filter(names).exclude(source=p.source)
              .values("id", "source", "package_id", "platform", "version")
              .order_by("source", "package_id")]
    seen, also = set(), []
    for o in others:
        key = (o["source"], o["package_id"])
        if key not in seen:
            seen.add(key)
            also.append({**o, "source_label": SOURCE_LABELS.get(o["source"], o["source"])})
    counts = _installed_counts(request.user, {(p.source, p.package_id)} | seen)
    return Response({
        **_card(p, counts.get((p.source, p.package_id), 0)),
        "description": p.description, "homepage": p.homepage, "license": p.license,
        "releases": releases,
        "also": [{**a, "installed": counts.get((a["source"], a["package_id"]), 0)} for a in also],
    })


def _icon_dir() -> Path:
    return Path(settings.MEDIA_ROOT) / "catalog-icons"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def icon(request, package_id):
    """The package's icon, fetched once from its allowlisted host and cached:
    the page's CSP only loads images from Vigil itself."""
    p = get_object_or_404(CatalogPackage, pk=package_id)
    if not p.icon_url:
        return HttpResponse(status=404)
    path = _icon_dir() / (hashlib.sha256(p.icon_url.encode()).hexdigest() + ".png")
    if not path.exists():
        if not config.setting("VIGIL_CATALOG_SYNC"):
            return HttpResponse(status=404)   # never reach out while the catalog is off
        try:
            data = fetch(p.icon_url, max_bytes=MAX_ICON_BYTES)
        except FetchError:
            return HttpResponse(status=404)
        if not data.startswith(_PNG):
            return HttpResponse(status=404)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    response = FileResponse(path.open("rb"), content_type="image/png")
    response["Cache-Control"] = "private, max-age=604800"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _status_body() -> dict:
    return {
        "enabled": bool(config.setting("VIGIL_CATALOG_SYNC")),
        "locked": config.env_locked("VIGIL_CATALOG_SYNC"),
        "syncs": [{"source": s.source, "source_label": SOURCE_LABELS.get(s.source, s.source),
                   "release": s.release, "count": s.count, "error": s.error,
                   "synced_at": s.synced_at.isoformat() if s.synced_at else None}
                  for s in CatalogSync.objects.order_by("source", "release")],
        "packages": CatalogPackage.objects.count(),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def status(request):
    return Response(_status_body())


def _sync_in_background():
    """Celery when it is there; a thread otherwise (a dev server, a test
    instance) — the first sync should not wait a day for beat."""
    from .tasks import sync_catalog
    try:
        sync_catalog.delay()
        return
    except Exception:   # noqa: BLE001 — no broker: run it here instead
        pass
    threading.Thread(target=sync_catalog, daemon=True).start()


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def enable(request):
    """Turn the catalog on (or off with {"enabled": false}) and start a sync."""
    on = request.data.get("enabled", True) is not False
    if config.env_locked("VIGIL_CATALOG_SYNC"):
        return Response({"detail": "VIGIL_CATALOG_SYNC is set in the environment; change it there."},
                        status=409)
    config.write("VIGIL_CATALOG_SYNC", "true" if on else "false", user=request.user)
    if on:
        _sync_in_background()
    return Response(_status_body())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def install_task(request):
    """{packages: [catalog ids]} → a task that installs the app: one
    app_install step per package manager, each guarded to the hosts that have
    that manager. The user's own "Install <name>" task is reused, so picking
    the same app again does not pile up copies. Deploying it is the normal
    Deploy dialog (targets, schedule, TOTP)."""
    import yaml

    from apps.software.guards import source_guard
    from apps.tasks.authz import author_denied
    from apps.tasks.models import TaskDefinition
    from apps.tasks.spec import SpecError
    from apps.tasks.views import _save_definition_from_yaml

    if denied := author_denied(request.user):
        return denied
    ids = request.data.get("packages")
    if not isinstance(ids, list) or not ids or len(ids) > 10:
        return Response({"error": "packages must be a list of 1 to 10 catalog ids"}, status=400)
    try:
        order = [int(i) for i in ids]
        packages = sorted(CatalogPackage.objects.filter(id__in=order), key=lambda p: order.index(p.id))
    except (TypeError, ValueError):
        return Response({"error": "packages must be catalog ids"}, status=400)
    if not packages:
        return Response({"error": "none of those packages are in the catalog"}, status=404)
    name = packages[0].name
    steps, seen = [], set()
    for p in packages:
        if (p.source, p.package_id) in seen:
            continue
        seen.add((p.source, p.package_id))
        step = {"id": f"install-{len(steps) + 1}", "type": "app_install"}
        if guard := source_guard(p.source):
            step["when"] = guard
        step["params"] = {"app": p.package_id, "source": p.source}
        steps.append(step)
    title = f"Install {name}"[:120]
    doc = {"name": title,
           "description": f"Installs {name} from the Store: "
                          + ", ".join(f"{SOURCE_LABELS.get(p.source, p.source)} {p.package_id}"
                                      for p in packages)
                          + ". Each step runs only where its package manager is.",
           "risk": "standard", "actions": steps}
    definition = (TaskDefinition.objects.filter(owner=request.user, name=title, archived_at__isnull=True)
                  .first() or TaskDefinition(owner=request.user,
                                             visibility=TaskDefinition.Visibility.PRIVATE))
    try:
        _save_definition_from_yaml(definition, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    except SpecError as exc:
        return Response({"error": str(exc)}, status=400)
    definition.save()
    return Response({"definition_id": str(definition.id), "name": definition.name}, status=201)
