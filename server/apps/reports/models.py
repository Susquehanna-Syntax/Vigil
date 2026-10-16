"""Saved reports: a named set of playbooks/automations, a range and charts.

The report itself is just a saved selection (QA-42 draws it); every number on
the page is computed from run history at request time by apps.reports.widgets.
"""

import uuid

from django.conf import settings
from django.db import models


class Report(models.Model):
    #: The ranges the builder offers. Deliberately a fixed list so a report's
    #: window is always one the charts were designed for.
    RANGES = (7, 14, 30, 90, 180, 365)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="reports")
    playbooks = models.ManyToManyField("baselines.Playbook", blank=True,
                                       related_name="reports")
    automations = models.ManyToManyField("automations.Automation", blank=True,
                                         related_name="reports")
    range_days = models.PositiveIntegerField(default=30)
    #: [{"kind": <a widgets.WIDGETS key>, "title": "<optional, max 80>"}], max 12, in display order.
    widgets = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name
