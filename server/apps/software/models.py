import re
from collections.abc import Sequence

from django.db import models
from django.db.models.constraints import BaseConstraint
from django.db.models.indexes import Index

#: Trailing architecture tag, alone or leading a parenthesised group
#: (``(x64 en-US)``, ``x64``). Stripped only from the end of the name.
_ARCH_TAG = re.compile(
    r"\s*\(?\s*(?:x64|x86|64-bit|32-bit)(?:\s[^)]*)?\)\s*$", re.IGNORECASE
)

#: Trailing version token — needs a dot, so a lone year in
#: "Microsoft Visual C++ 2015-2022 Redistributable" survives.
_VERSION_TAG = re.compile(r"\s+v?\d+(?:\.\d+)+(?:\s.*)?$")


def name_key(name: str) -> str:
    """Canonical grouping key for a display name.

    Lowercase, with a trailing architecture tag and a trailing dotted version
    token removed and whitespace collapsed. Deliberately dumb: "7-Zip" keeps
    its leading 7, "Notepad++" keeps its pluses.
    """
    key = (name or "").strip().lower()
    key = _ARCH_TAG.sub("", key)
    key = _VERSION_TAG.sub("", key)
    return re.sub(r"\s+", " ", key).strip()


# Frozen so the type checker does not read the Meta lists below as mutable
# class-attribute defaults.
_CONSTRAINTS: Sequence[BaseConstraint] = (
    models.UniqueConstraint(
        fields=("host", "source", "package_id", "scope", "user"),
        name="uniq_software_item",
    ),
)

_INDEXES: Sequence[Index] = (
    models.Index(fields=("host", "name_key")),
)


class SoftwareItem(models.Model):
    class Source(models.TextChoices):
        DPKG = "dpkg", "dpkg"
        RPM = "rpm", "rpm"
        APK = "apk", "apk"
        PACMAN = "pacman", "pacman"
        FLATPAK = "flatpak", "flatpak"
        SNAP = "snap", "snap"
        WINGET = "winget", "winget"
        CHOCOLATEY = "chocolatey", "chocolatey"
        SCOOP = "scoop", "scoop"
        REGISTRY = "registry", "registry"
        WINDOWS_UPDATE = "windows_update", "Windows Update"
        BREW = "brew", "brew"

    class Scope(models.TextChoices):
        MACHINE = "machine", "Machine"
        USER = "user", "User"

    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE, related_name="software")
    source = models.CharField(max_length=20, choices=Source.choices)
    package_id = models.CharField(max_length=300)
    name = models.CharField(max_length=300)
    name_key = models.CharField(max_length=300, db_index=True)
    version = models.CharField(max_length=120, blank=True)
    latest_version = models.CharField(max_length=120, blank=True)
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.MACHINE)
    user = models.CharField(max_length=120, blank=True)
    publisher = models.CharField(max_length=200, blank=True)
    managed = models.BooleanField(default=True)
    first_seen = models.DateTimeField()
    #: When the item last became outdated; null while it is current. How long
    #: a host has been missing an update is measured from here.
    outdated_since = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = _CONSTRAINTS
        indexes = _INDEXES

    def __str__(self):
        return f"{self.name} {self.version} ({self.source})"

    @property
    def outdated(self) -> bool:
        return bool(self.latest_version) and self.latest_version != self.version


class SoftwareSnapshot(models.Model):
    host = models.OneToOneField(
        "hosts.Host", on_delete=models.CASCADE, related_name="software_snapshot"
    )
    digest = models.CharField(max_length=64, blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now=True)
    item_count = models.IntegerField(default=0)
    errors = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"software snapshot for {self.host_id} ({self.item_count})"


class PendingUpdate(models.Model):
    """One update a host is missing — the rows behind the fleet view by update.

    Windows rows come from the agent's per-update list at check-in (key = the
    KB, or the update id when it has none). Linux rows mirror the host's
    outdated primary-manager packages (key = the package id). Replaced per
    report; ``first_seen`` survives, so the age of a missing update is real.
    """

    class Kind(models.TextChoices):
        WINDOWS = "windows", "Windows Update"
        LINUX = "linux", "Linux package"

    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE,
                             related_name="pending_updates")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    key = models.CharField(max_length=300)
    title = models.CharField(max_length=300, blank=True)
    severity = models.CharField(max_length=20, blank=True)
    classification = models.CharField(max_length=60, blank=True)
    reboot_required = models.BooleanField(default=False)
    #: Linux: the version that would install.
    version = models.CharField(max_length=120, blank=True)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("host", "kind", "key"),
                                               name="uniq_pending_update")]
        indexes = [models.Index(fields=("kind", "key"))]

    def __str__(self):
        return f"{self.key} on {self.host_id}"
