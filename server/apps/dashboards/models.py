"""Dashboards — a named grid of widgets belonging to one operator."""

import uuid

from django.conf import settings
from django.db import models

from .widgets import clean_settings, default_settings


class Dashboard(models.Model):
    """One arrangement of widgets, owned by one user.

    A user may keep several — "Overview", "NOC wall", one per client — and
    exactly one of theirs is the landing dashboard.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="dashboards")
    name = models.CharField(max_length=120)
    #: The one this owner lands on. Enforced per owner by save(), not by a
    #: constraint: clearing the old default and setting the new one cannot be
    #: expressed as a single row's invariant.
    is_default = models.BooleanField(default=False)
    #: Business. A shared dashboard is readable by every user on the instance.
    #: The flag lives here in core because all migrations always run; the
    #: endpoint that sets it is what the licence gates.
    shared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=("owner", "name"),
                                    name="uniq_dashboard_name_per_owner"),
        ]

    def __str__(self) -> str:
        return f"dashboard:{self.name}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_default:
            Dashboard.objects.filter(owner=self.owner).exclude(
                pk=self.pk).update(is_default=False)


class DashboardWidget(models.Model):
    """One widget placed on a dashboard.

    Geometry is Gridstack's: x/y are grid units from the top-left, w/h a span
    on a 12-column grid. Stored as columns rather than a JSON blob so a layout
    can be queried and a bad one is visible in the admin.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE,
                                  related_name="widgets")
    kind = models.CharField(max_length=40)
    x = models.PositiveIntegerField(default=0)
    y = models.PositiveIntegerField(default=0)
    w = models.PositiveIntegerField(default=6)
    h = models.PositiveIntegerField(default=4)
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["y", "x"]

    def __str__(self) -> str:
        return f"{self.kind}@{self.x},{self.y}"

    def save(self, *args, **kwargs):
        # The registry is the only thing that says what a widget may hold, and
        # settings arrive as operator JSON.
        self.settings = clean_settings(self.kind, self.settings)
        super().save(*args, **kwargs)


#: What a user's first dashboard holds — roughly today's fixed dashboard.
STARTER_WIDGETS = [
    ("stat_tile", 0, 0, 3, 2, {"stat": "hosts"}),
    ("stat_tile", 3, 0, 3, 2, {"stat": "online"}),
    ("stat_tile", 6, 0, 3, 2, {"stat": "alerts_firing"}),
    ("stat_tile", 9, 0, 3, 2, {"stat": "pending"}),
    ("host_status_grid", 0, 2, 12, 5, {}),
    ("alert_list", 0, 7, 6, 4, {}),
    ("rollout_progress", 6, 7, 6, 3, {}),
]


def starter_dashboard(user):
    """Create this user's first dashboard. Returns it."""
    board = Dashboard.objects.create(owner=user, name="Overview", is_default=True)
    for kind, x, y, w, h, overrides in STARTER_WIDGETS:
        DashboardWidget.objects.create(
            dashboard=board, kind=kind, x=x, y=y, w=w, h=h,
            settings={**default_settings(kind), **overrides})
    return board
