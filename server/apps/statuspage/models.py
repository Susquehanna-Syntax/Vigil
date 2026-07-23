"""Status pages — the basic page is free; branding and per-site pages are
Business (``status_branding``).

The free page is a real, public, shareable status page (token URL, all hosts
or a chosen subset) with a "Powered by Vigil" badge — a homelabber's status
page is self-accountability and the badge is the marketing budget (§2).
Business removes the badge, sets a custom title/logo, and adds *additional*
pages scoped to a site — a client-facing page per client is exactly the
"someone outside the org is looking at it" accountability line (§0/§1).
"""

import secrets
import uuid

from django.db import models


def _token() -> str:
    return secrets.token_urlsafe(16)


class HostUptimeSample(models.Model):
    """A periodic up/down reading for a host, sampled by a beat task.

    Availability history for the status page's uptime bars. One row per host
    per sample interval; ``up`` is True when the host was Online at sample
    time. Kept in the status-page app so core ``hosts`` gains no columns.
    """

    host = models.ForeignKey(
        "hosts.Host", on_delete=models.CASCADE, related_name="uptime_samples")
    time = models.DateTimeField(db_index=True)
    up = models.BooleanField()

    class Meta:
        indexes = [models.Index(fields=["host", "time"])]

    def __str__(self) -> str:
        return f"{self.host_id} {'up' if self.up else 'down'} @ {self.time}"


class StatusPage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # The unguessable URL slug. Rotatable by saving a new one.
    token = models.CharField(max_length=64, unique=True, default=_token)
    title = models.CharField(max_length=200, default="Service status")
    enabled = models.BooleanField(default=False)
    # Empty = every non-pending host; else host UUID strings.
    host_ids = models.JSONField(default=list, blank=True)
    # Optional per-host display-name overrides: {host_id: "Public name"}.
    # A client-facing page shouldn't have to expose internal hostnames.
    host_labels = models.JSONField(default=dict, blank=True)
    # Business scoping/branding. site_id references business_sites.Site but is
    # a plain UUID (no FK) so the free page never joins a Business table.
    site_id = models.UUIDField(null=True, blank=True)
    logo_url = models.URLField(blank=True, default="")
    hide_badge = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"status:{self.title}"

    @property
    def is_primary(self) -> bool:
        """The oldest page is the free one; extra pages are Business."""
        first = StatusPage.objects.order_by("created_at").values_list(
            "id", flat=True).first()
        return first == self.id

    def hosts(self):
        from apps.hosts.models import Host

        qs = Host.objects.exclude(status=Host.Status.PENDING).exclude(
            status=Host.Status.REJECTED)
        if self.host_ids:
            qs = qs.filter(id__in=self.host_ids)
        elif self.site_id:
            qs = qs.filter(site_assignment__site_id=self.site_id)
        return qs.order_by("hostname")
