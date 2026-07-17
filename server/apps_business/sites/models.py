import uuid

from django.db import models


class Site(models.Model):
    """An administrative boundary — a campus, a department, a client org.

    Not a physical location. Free installs have exactly one (the default,
    created by migration); a Business license lifts the limit.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, unique=True)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True, default="")
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class HostSiteAssignment(models.Model):
    """Places a core Host in a Site. A host with no row is in the default site."""
    host = models.OneToOneField(
        "hosts.Host", on_delete=models.CASCADE, related_name="site_assignment",
    )
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="host_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)
