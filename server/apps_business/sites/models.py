import uuid

from django.db import models


class SiteManager(models.Manager):
    def global_site(self):
        """The single scope that cascades into every other site."""
        return self.get(is_global=True)


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
    # The scope whose baselines/automations/channels cascade into every site.
    # Holds no hosts. Exactly one row carries this flag.
    is_global = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SiteManager()

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        # Both are structural: the default site is where unassigned hosts
        # live, and the global site is where cascading policy lives. Callers
        # in the API layer check these first and answer 400.
        if self.is_global:
            raise ValueError("The global site cannot be deleted.")
        if self.is_default:
            raise ValueError("The default site cannot be deleted.")
        return super().delete(*args, **kwargs)


class HostSiteAssignment(models.Model):
    """Places a core Host in a Site. A host with no row is in the default site."""
    host = models.OneToOneField(
        "hosts.Host", on_delete=models.CASCADE, related_name="site_assignment",
    )
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="host_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)
