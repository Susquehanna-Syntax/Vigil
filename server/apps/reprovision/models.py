"""Remote OS reprovisioning. See docs/reprovisioning.md.

The job state machine outlives the tasks it dispatches: a rebuild has the
agent vanish for ~40 minutes and return as a different operating system,
which the request/response Task model cannot express.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.hosts.models import Host


class OSImage(models.Model):
    """A catalog entry.

    The ISO is discarded after import (§6) — the extracted tree and the
    kernel/initrd pair are what get served; the digest is the record that the
    bytes were what they claimed to be.
    """

    class Family(models.TextChoices):
        UBUNTU = "ubuntu", "Ubuntu"
        DEBIAN = "debian", "Debian"
        RHEL = "rhel", "RHEL family"

    class Status(models.TextChoices):
        IMPORTING = "importing", "Importing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    os_family = models.CharField(max_length=16, choices=Family.choices)
    version = models.CharField(max_length=40)
    architecture = models.CharField(max_length=32, default="x86_64")
    sha256 = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.IMPORTING)
    import_error = models.TextField(blank=True)
    kernel_path = models.CharField(max_length=512, blank=True)
    initrd_path = models.CharField(max_length=512, blank=True)
    tree_path = models.CharField(max_length=512, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL,
                                   related_name="os_images")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["os_family", "-version"]

    def __str__(self):
        return f"{self.name} ({self.architecture})"


class InstallProfile(models.Model):
    """Typed settings for an image, plus a verbatim escape hatch (§6)."""

    class NetworkMode(models.TextChoices):
        DHCP = "dhcp", "DHCP"
        STATIC = "static", "Static"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    image = models.ForeignKey(OSImage, on_delete=models.CASCADE,
                              related_name="profiles")
    disk_target = models.CharField(max_length=120)
    partition_scheme = models.CharField(max_length=40, default="lvm")
    filesystem = models.CharField(max_length=20, default="ext4")
    network_mode = models.CharField(max_length=10, choices=NetworkMode.choices,
                                    default=NetworkMode.DHCP)
    static_address = models.CharField(max_length=64, blank=True)
    gateway = models.CharField(max_length=64, blank=True)
    dns = models.CharField(max_length=160, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    locale = models.CharField(max_length=32, default="en_US.UTF-8")
    keyboard = models.CharField(max_length=32, default="us")
    admin_username = models.CharField(max_length=64)
    # Crypt hash of the admin password — never plaintext, and the answer file
    # only ever carries the hash (§4.2). Encrypted at rest on top of that.
    admin_password_encrypted = models.BinaryField(blank=True, default=b"")
    ssh_authorized_keys = models.TextField(blank=True)
    extra_packages = models.JSONField(default=list, blank=True)
    raw_append = models.TextField(blank=True)
    deadline_minutes = models.IntegerField(default=60)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL,
                                   related_name="install_profiles")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class RebuildJob(models.Model):
    """One host, one rebuild. See §3.3 for the state machine."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        STAGING = "staging", "Staging"
        STAGED = "staged", "Staged"
        REBOOTING = "rebooting", "Rebooting"
        INSTALLING = "installing", "Installing"
        ENROLLING = "enrolling", "Enrolling"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        ABORTED = "aborted", "Aborted"
        TIMED_OUT = "timed_out", "Timed out"

    #: States from which nothing further happens.
    TERMINAL_STATES = frozenset({"completed", "failed", "aborted", "timed_out"})

    #: States in which the answer file is servable (§4.2). Outside this the
    #: URL 404s, including after the job completes.
    ANSWER_SERVABLE_STATES = frozenset({"rebooting", "installing"})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(Host, on_delete=models.CASCADE,
                             related_name="rebuild_jobs")
    # PROTECT, not CASCADE: deleting an image or profile mid-rebuild would
    # strand an installer that is already wiping a disk.
    image = models.ForeignKey(OSImage, on_delete=models.PROTECT,
                              related_name="jobs")
    profile = models.ForeignKey(InstallProfile, on_delete=models.PROTECT,
                                related_name="jobs")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                     on_delete=models.SET_NULL,
                                     related_name="rebuild_jobs")
    requested_at = models.DateTimeField(auto_now_add=True)
    confirmed_ip = models.GenericIPAddressField(null=True, blank=True)

    state = models.CharField(max_length=12, choices=State.choices,
                             default=State.PENDING)
    state_changed_at = models.DateTimeField(auto_now_add=True)
    deadline = models.DateTimeField()
    failure_reason = models.TextField(blank=True)

    # Tokens are stored hashed only (§4.2): a database read must not yield a
    # usable credential. 256-bit random values, so a plain SHA-256 is right —
    # there is nothing to brute-force and no need for a slow KDF.
    answer_token_hash = models.CharField(max_length=64, db_index=True, blank=True)
    answer_fetched_at = models.DateTimeField(null=True, blank=True)
    answer_fetch_ip = models.GenericIPAddressField(null=True, blank=True)
    enroll_token_hash = models.CharField(max_length=64, db_index=True, blank=True)
    enroll_consumed_at = models.DateTimeField(null=True, blank=True)

    preflight = models.JSONField(default=dict, blank=True)
    completion_tag = models.CharField(max_length=120, blank=True)
    post_baseline = models.ForeignKey(
        "baselines.Baseline", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="rebuild_jobs")
    post_run = models.ForeignKey(
        "tasks.TaskRun", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="rebuild_jobs")

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"rebuild {self.host.hostname} → {self.image.name} [{self.state}]"

    @property
    def is_terminal(self) -> bool:
        return self.state in self.TERMINAL_STATES
