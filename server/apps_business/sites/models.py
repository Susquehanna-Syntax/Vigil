import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
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
    # The one structural scope: it holds every unassigned host, and its
    # baselines/automations/channels cascade into every other site. Exactly
    # one row carries this flag.
    is_global = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SiteManager()

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        # Structural: it holds every unassigned host and is the root that
        # cascading policy resolves from. The API checks this first and
        # answers 400.
        if self.is_global:
            raise ValueError("The global site cannot be deleted.")
        return super().delete(*args, **kwargs)


class HostSiteAssignment(models.Model):
    """Places a core Host in a Site. A host with no row is in the default site."""
    host = models.OneToOneField(
        "hosts.Host", on_delete=models.CASCADE, related_name="site_assignment",
    )
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="host_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)


class BaselineSiteAssignment(models.Model):
    """Places a core Baseline in a Site. No row means the global scope."""
    baseline = models.OneToOneField(
        "baselines.Baseline", on_delete=models.CASCADE, related_name="site_assignment")
    site = models.ForeignKey(Site, on_delete=models.CASCADE,
                             related_name="baseline_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)


class AutomationSiteAssignment(models.Model):
    """Places a core Automation in a Site. No row means the global scope."""
    automation = models.OneToOneField(
        "automations.Automation", on_delete=models.CASCADE, related_name="site_assignment")
    site = models.ForeignKey(Site, on_delete=models.CASCADE,
                             related_name="automation_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)


class ChannelSiteAssignment(models.Model):
    """Places a core NotificationChannel in a Site. No row means global."""
    channel = models.OneToOneField(
        "alerts.NotificationChannel", on_delete=models.CASCADE,
        related_name="site_assignment")
    site = models.ForeignKey(Site, on_delete=models.CASCADE,
                             related_name="channel_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)


class GlobalSuppression(models.Model):
    """This site opts out of one global resource. Advisory only — suppressing
    never edits or deletes the resource, which stays intact everywhere else."""
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="suppressions")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    resource = GenericForeignKey("content_type", "object_id")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("site", "content_type", "object_id")


class UserSiteRole(models.Model):
    """This user's role in this site. A user with NO rows falls back to their
    UserProfile.role everywhere; a user with ANY row is restricted to the
    sites named by their rows."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="site_roles")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="user_roles")
    role = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "site")


class SiteCapability(models.Model):
    """One granted verb for one operator in one site.

    Rows exist only for granted capabilities — absence is denial. Hung off
    UserSiteRole rather than (user, site) so revoking a role takes its
    capabilities with it by cascade, leaving no orphaned grants.
    """
    user_site_role = models.ForeignKey(
        UserSiteRole, on_delete=models.CASCADE, related_name="capabilities")
    app = models.CharField(max_length=20)
    verb = models.CharField(max_length=20)

    class Meta:
        unique_together = ("user_site_role", "app", "verb")

    def __str__(self):
        return f"{self.app}:{self.verb}"

    def clean(self):
        # Validated against the core vocabulary so a typo cannot be stored.
        from django.core.exceptions import ValidationError

        from apps.accounts.permissions import CAPABILITIES
        verbs = CAPABILITIES.get(self.app)
        if verbs is None or self.verb not in verbs:
            raise ValidationError(f"unknown capability {self.app}:{self.verb}")
