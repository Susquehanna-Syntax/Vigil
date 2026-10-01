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
    #: The compose file's name in working_dir — compose.yaml for a stack Vigil
    #: created, the stack's own file name for an adopted one.
    compose_file = models.CharField(max_length=100, default="compose.yaml")
    #: What adoption found: {"match": [services], "recreate": [services]} — the
    #: services a deploy of this file would leave alone, and those it would
    #: recreate (their running config differs from the file's).
    adopt_report = models.JSONField(default=dict, blank=True)
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


class EnvTicket(models.Model):
    """A one-time reference to one revision's .env (M11).

    A deploy task carries only this id. The agent redeems it once, over its
    own authenticated connection, just before it writes the .env; after that,
    or after it expires, it is worthless. Task params are plaintext JSON —
    signing is not encryption — so the secrets themselves never ride in one.
    """

    TTL_HOURS = 24

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stack = models.ForeignKey(ManagedStack, on_delete=models.CASCADE, related_name="env_tickets")
    revision = models.PositiveIntegerField()
    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class AdoptTicket(models.Model):
    """A one-time slot for an agent to hand over an existing stack's files
    (M11) — over its own connection, because the .env holds secrets."""

    TTL_HOURS = 24

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE, related_name="+")
    project = models.CharField(max_length=63)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                     related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    #: Why the files were refused, when they were (a compose file Vigil will
    #: not run, more than one compose file, …). Blank on success.
    error = models.CharField(max_length=500, blank=True)


class RegistryCredential(models.Model):
    """A private registry login (M11), encrypted at rest.

    Pulls through the engine API send it as X-Registry-Auth. An agent can
    fetch only the credentials for hosts this row covers — by tag, or every
    managed host when it names none — so one compromised host cannot read
    every registry password in the fleet.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The registry host as an image names it: ghcr.io, registry.example.com:5000.
    #: docker.io for Docker Hub.
    registry = models.CharField(max_length=255)
    username = models.CharField(max_length=255)
    password_encrypted = models.BinaryField()
    host_tags = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("registry",)
