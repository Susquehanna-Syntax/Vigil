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
