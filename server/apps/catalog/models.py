"""The package catalog behind the Store page (QA-26).

What each package manager *offers*, as opposed to apps.software, which records
what is *installed*. Filled by sync.py from the public indexes, only for the
sources and releases the fleet uses.
"""
from django.db import models


class CatalogPackage(models.Model):
    class Platform(models.TextChoices):
        WINDOWS = "windows", "Windows"
        LINUX = "linux", "Linux"
        MACOS = "macos", "macOS"

    #: The package manager: an apps.software Source value (winget, flatpak, dpkg …).
    source = models.CharField(max_length=20)
    #: "" for a source that is the same everywhere (winget, Flathub); the
    #: distro release for a repository ("ubuntu:noble", "debian:bookworm").
    release = models.CharField(max_length=40, blank=True, default="")
    #: What app_install's ``app`` takes for this source.
    package_id = models.CharField(max_length=300)
    name = models.CharField(max_length=300)
    summary = models.CharField(max_length=300, blank=True, default="")
    description = models.TextField(blank=True, default="")
    version = models.CharField(max_length=120, blank=True, default="")
    publisher = models.CharField(max_length=200, blank=True, default="")
    homepage = models.URLField(max_length=500, blank=True, default="")
    license = models.CharField(max_length=200, blank=True, default="")
    categories = models.JSONField(default=list, blank=True)
    #: Where the icon is fetched from (an allowlisted host); served same-origin
    #: by views.icon, which caches it. Blank = a letter tile.
    icon_url = models.URLField(max_length=500, blank=True, default="")
    platform = models.CharField(max_length=10, choices=Platform.choices)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source", "release", "package_id"),
                                    name="uniq_catalog_package"),
        ]
        indexes = [models.Index(fields=("name",)), models.Index(fields=("platform", "source"))]

    def __str__(self) -> str:
        return f"{self.source}:{self.package_id}"


class CatalogSync(models.Model):
    """One fetch of one index: when, how many packages, and what went wrong."""

    source = models.CharField(max_length=20)
    release = models.CharField(max_length=40, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    count = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source", "release"), name="uniq_catalog_sync"),
        ]
