import uuid

from django.conf import settings
from django.db import models

from apps.hosts.models import Host


class VulnSummary(models.Model):
    """Per-host vulnerability summary synced from Nessus / Tenable."""

    host = models.OneToOneField(Host, on_delete=models.CASCADE, related_name="vuln_summary")
    last_scan_at = models.DateTimeField(null=True, blank=True)
    scanner_scan_id = models.IntegerField(null=True, blank=True)
    critical = models.IntegerField(default=0)
    high = models.IntegerField(default=0)
    medium = models.IntegerField(default=0)
    low = models.IntegerField(default=0)
    info = models.IntegerField(default=0)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-critical", "-high"]
        verbose_name = "Vulnerability Summary"
        verbose_name_plural = "Vulnerability Summaries"

    def __str__(self):
        return f"Vulns: {self.host.hostname} (C:{self.critical}/H:{self.high}/M:{self.medium})"


class VulnScan(models.Model):
    """A request or in-flight Nessus scan against a single host.

    Lifecycle::

        REQUESTED → LAUNCHED → RUNNING → COMPLETED   (success)
                                       → FAILED      (Nessus errored)
                                       → ABORTED     (Nessus or admin canceled)

    Created either by the "Scan now" UI (with ``requested_by`` set) or by
    the ``request_nessus_scan`` task action (``requested_by`` = None,
    ``requested_via_task`` = True).
    """

    class State(models.TextChoices):
        REQUESTED = "requested", "Requested"
        LAUNCHED = "launched", "Launched"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        ABORTED = "aborted", "Aborted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey(Host, on_delete=models.CASCADE, related_name="vuln_scans")
    state = models.CharField(max_length=16, choices=State.choices, default=State.REQUESTED)
    nessus_scan_id = models.IntegerField(null=True, blank=True)
    target = models.CharField(max_length=255, default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    launched_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="vuln_scans_requested",
    )
    # True when the scan request came from an agent running the
    # request_nessus_scan task action (no human requester).
    requested_via_task = models.BooleanField(default=False)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["host", "-requested_at"]),
            models.Index(fields=["state"]),
        ]

    @property
    def is_active(self) -> bool:
        return self.state in {self.State.REQUESTED, self.State.LAUNCHED, self.State.RUNNING}

    def __str__(self):
        return f"VulnScan[{self.state}] {self.host.hostname}"
