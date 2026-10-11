"""Fill the catalog from the public indexes, for what the fleet runs.

winget when any host runs Windows, Flathub when any runs Linux, and one apt
release per Ubuntu/Debian version (and architecture) in the inventory. Each
index is one CatalogSync row; a failure is recorded there and leaves that
index's packages as they were.
"""
import logging

from django.db import transaction
from django.utils.timezone import now

from .download import FetchError, fetch
from .models import CatalogPackage, CatalogSync
from .sources import apt, flathub, winget

log = logging.getLogger("vigil.catalog")

_FIELDS = ("name", "summary", "description", "version", "publisher", "homepage", "license",
           "categories", "icon_url")


def wanted() -> list[tuple]:
    """(module, release, [urls]) for each index the fleet needs."""
    from apps.hosts.models import Host, HostInventory

    hosts = Host.objects.exclude(status__in=("rejected", "pending"))
    oses = " ".join(hosts.values_list("os", flat=True)).lower()
    out = []
    if "windows" in oses:
        out.append((winget, "", [winget.URL]))
    releases = set()
    for os_name, os_version, arch in (HostInventory.objects.filter(host__in=hosts)
                                      .values_list("os_name", "os_version", "architecture")):
        rel = apt.release_for(os_name, os_version)
        if rel:
            releases.add((rel, "arm64" if (arch or "").lower() in ("arm64", "aarch64") else "amd64"))
    if releases or any(w in oses for w in ("linux", "ubuntu", "debian", "fedora", "arch", "alpine")):
        out.append((flathub, "", [flathub.URL]))
    for rel, arch in sorted(releases):
        out.append((apt, rel if arch == "amd64" else f"{rel}:{arch}", apt.urls(rel, arch)))
    return out


def store(module, release: str, packages) -> int:
    """Replace one index's packages with ``packages``; returns how many."""
    incoming = {}
    for pkg in packages:
        incoming[pkg["package_id"]] = pkg   # a later stanza (e.g. universe) wins
    with transaction.atomic():
        existing = {p.package_id: p for p in CatalogPackage.objects.filter(
            source=module.SOURCE, release=release)}
        create, update = [], []
        for pid, pkg in incoming.items():
            row = existing.pop(pid, None)
            values = {f: pkg.get(f, [] if f == "categories" else "") for f in _FIELDS}
            if row is None:
                create.append(CatalogPackage(source=module.SOURCE, release=release, package_id=pid,
                                             platform=module.PLATFORM, **values))
            elif any(getattr(row, f) != v for f, v in values.items()):
                for f, v in values.items():
                    setattr(row, f, v)
                update.append(row)
        CatalogPackage.objects.bulk_create(create, batch_size=1000)
        CatalogPackage.objects.bulk_update(update, _FIELDS, batch_size=1000)
        if existing:
            CatalogPackage.objects.filter(pk__in=[r.pk for r in existing.values()]).delete()
    return len(incoming)


def sync_one(module, release: str, urls: list[str]) -> CatalogSync:
    row, _ = CatalogSync.objects.get_or_create(source=module.SOURCE, release=release)
    try:
        packages = []
        for url in urls:
            packages.extend(module.parse(fetch(url), release))
        row.count = store(module, release, packages)
        row.error = ""
        row.synced_at = now()
    except (FetchError, ValueError, OSError, KeyError) as exc:
        log.warning("catalog sync of %s %s failed: %s", module.SOURCE, release, exc)
        row.error = str(exc)[:500]
    except Exception as exc:   # noqa: BLE001 — a parser surprise must not stop the other indexes
        log.exception("catalog sync of %s %s failed", module.SOURCE, release)
        row.error = f"unexpected error: {type(exc).__name__}"
    row.save()
    return row


def sync_all(only: str = "") -> list[CatalogSync]:
    return [sync_one(m, rel, urls) for m, rel, urls in wanted() if not only or m.SOURCE == only]
