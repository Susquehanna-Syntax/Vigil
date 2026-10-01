"""Vigil-managed compose stacks (M11): the compose file and its .env, kept
in Vigil and deployed to one host by signed tasks.

Every save is a revision, so a bad edit can be rolled back to a version that
worked. The .env is Fernet-encrypted at rest (apps/hosts/crypto.py) and
never travels in a task: a deploy carries a one-time reference the agent
redeems over its own authenticated connection (EnvTicket, phase 09).
"""

import uuid

from django.conf import settings
from django.db import models


class ManagedStack(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE, related_name="managed_stacks")
    #: The compose project name — also the folder under /opt/vigil/stacks/.
    name = models.CharField(max_length=63)
    compose_yaml = models.TextField()
    env_encrypted = models.BinaryField(blank=True, default=b"")
    revision = models.PositiveIntegerField(default=1)
    #: Where the stack lives on the host: /opt/vigil/stacks/<name> for one
    #: Vigil created; the stack's own folder for an adopted one.
    working_dir = models.CharField(max_length=500, blank=True)
    adopted = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("host", "name"), name="uniq_managed_stack")]
        ordering = ("name",)

    def __str__(self):
        return f"{self.name}@{self.host_id}"


class StackRevision(models.Model):
    stack = models.ForeignKey(ManagedStack, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField()
    compose_yaml = models.TextField()
    env_encrypted = models.BinaryField(blank=True, default=b"")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("stack", "number"), name="uniq_stack_revision")]
        ordering = ("-number",)
