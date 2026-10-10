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
    #: A stack whose compose file comes from a Git repository (2026.14.1).
    #: Blank = the compose file is edited in Vigil. Otherwise the repo is the
    #: source of truth: each pull that finds a new commit makes a revision
    #: recording that commit (StackRevision.git_commit), and the folder that
    #: holds the compose file is shipped to the host (SourceSnapshot) so
    #: ``build:`` contexts have their source. https:// or ssh only.
    git_url = models.CharField(max_length=500, blank=True)
    #: The branch to track; each deploy takes its latest commit.
    git_branch = models.CharField(max_length=200, blank=True, default="main")
    #: An exact commit (40 hex) or tag to hold the stack at instead. Blank =
    #: follow git_branch.
    git_pin = models.CharField(max_length=200, blank=True)
    #: The compose file's path inside the repo, e.g. deploy/compose.yaml. Its
    #: folder becomes the stack folder on the host.
    git_path = models.CharField(max_length=300, blank=True, default="compose.yaml")
    git_credential = models.ForeignKey("GitCredential", null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name="stacks")
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
    #: The commit this revision's compose file came from (Git stacks only).
    git_commit = models.CharField(max_length=64, blank=True)
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


class GitCredential(models.Model):
    """How Vigil reaches a private Git repository (2026.14.1), encrypted at rest.

    Only the server ever uses it: the server fetches the repo and hands the
    host a packed copy, so no host holds a repository credential. ``token`` is
    an HTTPS access token (GitHub, GitLab, Gitea …) sent through GIT_ASKPASS,
    never on a command line; ``ssh_key`` is a deploy key, used only with the
    host keys pinned in ``known_hosts`` — an unknown or changed host key is
    refused, not trusted on first use.
    """

    class Kind(models.TextChoices):
        TOKEN = "token", "HTTPS token"
        SSH_KEY = "ssh_key", "SSH deploy key"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    #: For a token: the user name the host expects (x-access-token, oauth2 …).
    username = models.CharField(max_length=255, blank=True)
    secret_encrypted = models.BinaryField()
    #: For an SSH key: known_hosts lines for the Git server(s).
    known_hosts = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return f"git-credential:{self.name} ({self.kind})"


class SourceSnapshot(models.Model):
    """The packed folder of one Git revision (tar.gz of regular files only).

    Built once by the server at pull time; a deploy's signed task carries its
    sha256, so the host can tell it got exactly this archive. Kept small
    (gitsource.MAX_ARCHIVE_BYTES) and pruned to the last few per stack.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stack = models.ForeignKey(ManagedStack, on_delete=models.CASCADE, related_name="snapshots")
    revision = models.PositiveIntegerField()
    commit = models.CharField(max_length=64)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveIntegerField()
    data = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)


class SourceTicket(models.Model):
    """A one-time reference to a SourceSnapshot for one deploy, like EnvTicket:
    redeemable once, by the stack's own host, before it expires."""

    TTL_HOURS = 24

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot = models.ForeignKey(SourceSnapshot, on_delete=models.CASCADE, related_name="tickets")
    host = models.ForeignKey("hosts.Host", on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
