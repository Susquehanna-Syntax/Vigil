"""OpenAPI shapes of the core managed-stack endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""

from rest_framework import serializers

# ── the stack row every endpoint returns ────────────────────────────────────


class StacksGitSerializer(serializers.Serializer):
    """The Git source of a stack's compose file — ``git`` is null for a stack
    edited in Vigil."""

    url = serializers.CharField()
    branch = serializers.CharField(allow_blank=True)
    pin = serializers.CharField(
        allow_blank=True, help_text="A commit to hold at; empty tracks the branch."
    )
    path = serializers.CharField(allow_blank=True)
    credential_id = serializers.CharField(allow_null=True)
    commit = serializers.CharField(
        allow_blank=True,
        help_text="The commit of the current revision; empty when its "
        "revision came from somewhere other than a pull.",
    )


class StacksEnvKeySerializer(serializers.Serializer):
    """One .env key, masked: ``set`` says whether a value is stored."""

    key = serializers.CharField()
    set = serializers.BooleanField()


class StacksStackSerializer(serializers.Serializer):
    """One managed stack as ``_row`` builds it.

    ``git`` is null for a stack edited in Vigil; ``host_id``/``hostname`` carry
    the machine it lives on, so the row needs no second lookup."""

    id = serializers.CharField()
    host_id = serializers.CharField()
    hostname = serializers.CharField()
    name = serializers.CharField()
    compose_yaml = serializers.CharField()
    revision = serializers.IntegerField()
    working_dir = serializers.CharField()
    adopted = serializers.BooleanField()
    compose_file = serializers.CharField()
    adopt_report = serializers.DictField(
        help_text="What the adoption agent reported ({match, recreate, …}); {} unless the stack was adopted."
    )
    git = StacksGitSerializer(allow_null=True)
    env = StacksEnvKeySerializer(many=True)
    updated_at = serializers.DateTimeField(allow_null=True)


class StacksStackListSerializer(serializers.Serializer):
    results = StacksStackSerializer(many=True)


# ── requests ────────────────────────────────────────────────────────────────


class StacksEnvEntrySerializer(serializers.Serializer):
    """One .env key on the way in: a new ``value``, or ``keep`` to hold the
    stored secret."""

    key = serializers.CharField()
    value = serializers.CharField(required=False)
    keep = serializers.BooleanField(required=False)


class StacksGitSettingsSerializer(serializers.Serializer):
    """The repository block a create or edit may carry."""

    url = serializers.CharField()
    branch = serializers.CharField(required=False)
    pin = serializers.CharField(required=False, help_text="A commit to hold at.")
    path = serializers.CharField(
        required=False, help_text="Where in the repo the compose file is."
    )
    credential_id = serializers.CharField(required=False, allow_null=True)


class StacksStackCreateSerializer(serializers.Serializer):
    """The POST body. Either ``compose_yaml`` or a ``git`` block is required;
    .env pairs come as ``env_text`` (the whole file) or ``env`` (key by key)."""

    host_id = serializers.CharField()
    name = serializers.CharField(help_text="Lowercase letters, digits, '_' or '-'.")
    compose_yaml = serializers.CharField(required=False)
    env_text = serializers.CharField(
        required=False, help_text="The whole .env file, KEY=value lines."
    )
    env = StacksEnvEntrySerializer(many=True, required=False)
    git = StacksGitSettingsSerializer(required=False)


class StacksStackEditSerializer(serializers.Serializer):
    """The PUT body: a field absent from it is left alone. ``compose_yaml`` is
    refused on a Git stack — that file comes from the repository."""

    compose_yaml = serializers.CharField(required=False)
    env_text = serializers.CharField(required=False)
    env = StacksEnvEntrySerializer(many=True, required=False)
    note = serializers.CharField(required=False)
    git = StacksGitSettingsSerializer(required=False)


class StacksValidateSerializer(serializers.Serializer):
    """The editor's live check. ``compose_yaml`` is the text as typed; a bad
    file is an answer (200 with ``ok`` false), never a failed request."""

    compose_yaml = serializers.CharField(required=False)
    env_keys = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="The .env keys the editor holds, so a reference one of "
        "them satisfies is not reported missing.",
    )


class StacksRevisionNameSerializer(serializers.Serializer):
    name = serializers.CharField(help_text='At most 80 characters; "" clears the name.')


class StacksRevisionsPruneSerializer(serializers.Serializer):
    """Either form; a ``keep`` below 1 or neither key is refused."""

    keep = serializers.IntegerField(
        required=False, help_text="Keep the newest N plus every named revision."
    )
    numbers = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="Delete exactly these revision numbers.",
    )


class StacksTotpSerializer(serializers.Serializer):
    """The second factor the gate consumes; a body carrying only this key is a
    valid reveal/deploy request."""

    totp = serializers.CharField(
        required=False, help_text="The current 6-digit code or a one-time token."
    )


class StacksDeployBodySerializer(StacksTotpSerializer):
    """A deploy's whole body: nothing beyond the confirmation is read. Git
    stacks pull their latest commit before the task is queued."""


class StacksRemoveBodySerializer(StacksTotpSerializer):
    delete_files = serializers.BooleanField(
        required=False,
        help_text="Take the stack's files off the host too; ignored on an adopted stack.",
    )


class StacksAdoptSerializer(StacksTotpSerializer):
    host_id = serializers.CharField()
    project = serializers.CharField(
        help_text="The compose project name as the host reports it."
    )


# ── responses ───────────────────────────────────────────────────────────────


class StacksComposeServiceSerializer(serializers.Serializer):
    """One service of a compose file, as the editor's side panel lists it."""

    name = serializers.CharField()
    image = serializers.CharField(allow_blank=True)
    ports = serializers.ListField(child=serializers.CharField())
    build = serializers.BooleanField()


class StacksComposeDescriptionSerializer(serializers.Serializer):
    """``describe_compose``'s answer, valid or not: on a refusal ``ok`` is
    false, ``error`` the full reason and ``line`` the line it refused (null
    when the refusal names none)."""

    ok = serializers.BooleanField()
    error = serializers.CharField(allow_blank=True)
    line = serializers.IntegerField(allow_null=True)
    services = StacksComposeServiceSerializer(many=True)
    env_refs = serializers.ListField(
        child=serializers.CharField(),
        help_text="Every $VAR/${VAR} reference in the text, sorted.",
    )
    missing_env = serializers.ListField(
        child=serializers.CharField(),
        help_text="Bare references nothing supplies a value for.",
    )


class StacksRevisionSerializer(serializers.Serializer):
    """One stored revision. ``env_keys`` are names only — a revision never
    ships its .env."""

    number = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)
    note = serializers.CharField(allow_blank=True)
    compose_yaml = serializers.CharField()
    git_commit = serializers.CharField(allow_blank=True)
    current = serializers.BooleanField()
    deployed = serializers.BooleanField()
    env_keys = serializers.ListField(child=serializers.CharField())
    created_by = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()


class StacksRevisionListSerializer(serializers.Serializer):
    results = StacksRevisionSerializer(
        many=True, help_text="The newest fifty revisions."
    )


class StacksPrunedSerializer(serializers.Serializer):
    deleted = serializers.ListField(
        child=serializers.IntegerField(),
        help_text="The numbers deleted, ascending; empty when nothing was "
        "doomed — the current revision and the one last deployed stay.",
    )
    remaining = serializers.IntegerField()


class StacksEnvValueSerializer(serializers.Serializer):
    key = serializers.CharField()
    value = serializers.CharField(allow_blank=True)


class StacksEnvRevealSerializer(serializers.Serializer):
    """The .env in the clear, for an editor that asked for it with a TOTP code."""

    env = StacksEnvValueSerializer(many=True)


class StacksTaskSerializer(serializers.Serializer):
    """The signed task queued for the stack's host, and the run it belongs to."""

    task = serializers.CharField()
    run = serializers.CharField()


class StacksDeploySerializer(StacksTaskSerializer):
    rebuilds = serializers.BooleanField(
        required=False,
        help_text="Only a Git stack says: false means the host's agent is too old to rebuild and "
        "the old image keeps running.",
    )


class StacksAdoptQueuedSerializer(serializers.Serializer):
    ticket = serializers.CharField(
        help_text="The one-time ticket the agent hands the files to."
    )
    task = serializers.CharField()


class StacksAgentHandoverSerializer(serializers.Serializer):
    """What the adoption agent posts: the files it read off the host."""

    project = serializers.CharField()
    compose = serializers.CharField()
    env = serializers.CharField(
        required=False, help_text="The whole .env file as it stands."
    )
    compose_file = serializers.CharField(
        required=False, help_text="Default compose.yaml."
    )
    working_dir = serializers.CharField(
        required=False, help_text="Absolute; refused otherwise."
    )
    hashes = serializers.DictField(
        required=False,
        help_text="Free-form per-service match/recreate report; stored as the "
        "stack's adopt_report, {} when absent.",
    )


class StacksOkSerializer(serializers.Serializer):
    ok = serializers.BooleanField()


class StacksAgentEnvSerializer(serializers.Serializer):
    """A redeemed deploy ticket: the .env for the revision deployed, as text."""

    env = serializers.CharField(help_text="KEY=value lines.")


class StacksErrorSerializer(serializers.Serializer):
    """The hand-built refusal these views speak: ``detail`` with one sentence,
    plus the 401 an unconfirmed request gets — both optional, since validation
    and prune refuse with only ``detail``, reveal/deploy/remove/adopt also with
    ``needs_totp``."""

    detail = serializers.CharField(required=False)
    needs_totp = serializers.BooleanField(required=False)


class StacksRevisionRenamedSerializer(serializers.Serializer):
    """The revision name PATCH's answer: the number it renamed and the stored
    name, whitespace already collapsed."""

    number = serializers.IntegerField()
    name = serializers.CharField(allow_blank=True)


# ── Git credentials, pulling, and private registries ────────────────────────


class StacksGitCredentialSerializer(serializers.Serializer):
    """One Git credential as ``_cred_row`` builds it — never the secret. The
    key is write-only; ``has_known_hosts`` says whether an SSH credential
    pinned the Git server's host keys, without echoing them."""

    id = serializers.CharField()
    name = serializers.CharField()
    # A plain field on purpose: ``choices=[…]`` would make this a named enum
    # component, and drf-spectacular resolves two *kind* choice sets that no
    # other app shares into one hash-named enum — a schema warning with no
    # view file to attribute it to.
    kind = serializers.CharField(help_text="``token`` or ``ssh_key``.")
    git_host = serializers.CharField(
        help_text="The one Git server this credential may be used with."
    )
    username = serializers.CharField(allow_blank=True)
    has_known_hosts = serializers.BooleanField()
    site_id = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()


class StacksGitCredentialListSerializer(serializers.Serializer):
    results = StacksGitCredentialSerializer(many=True)


class StacksGitCredentialCreateSerializer(serializers.Serializer):
    """The POST body. ``secret`` is the access token or the private key, and
    an ``ssh_key`` also needs the server's ``known_hosts`` line; ``totp`` is
    the confirmation asked for before anything is stored."""

    name = serializers.CharField()
    kind = serializers.CharField(help_text="``token`` or ``ssh_key``.")
    git_host = serializers.CharField(
        help_text="The Git server's host name only, e.g. github.com."
    )
    username = serializers.CharField(required=False)
    secret = serializers.CharField(help_text="An HTTPS access token or a private key.")
    known_hosts = serializers.CharField(
        required=False,
        help_text="A known_hosts line for the Git server; an SSH key needs one.",
    )
    site_id = serializers.CharField(required=False, allow_null=True)
    totp = serializers.CharField(required=False)


class StacksPullSerializer(serializers.Serializer):
    """The pull's answer: whether the repository brought anything new, and
    the stack row as it stands after."""

    changed = serializers.BooleanField(
        help_text="False when the repository is still at the commit the current revision came from."
    )
    stack = StacksStackSerializer()


class StacksRegistryCredentialSerializer(serializers.Serializer):
    """One registry login as ``_row`` builds it. The password is write-only,
    and ``host_tags`` are the host tags this credential covers — none means
    every managed host of its site."""

    id = serializers.CharField()
    registry = serializers.CharField(help_text="The registry host as an image names it.")
    username = serializers.CharField()
    host_tags = serializers.ListField(child=serializers.CharField())
    site_id = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()


class StacksRegistryCredentialListSerializer(serializers.Serializer):
    results = StacksRegistryCredentialSerializer(many=True)


class StacksRegistryCredentialCreateSerializer(serializers.Serializer):
    """The POST body; ``registry`` is lowercased before it is checked."""

    registry = serializers.CharField(help_text="ghcr.io, docker.io, registry.local:5000.")
    username = serializers.CharField()
    password = serializers.CharField()
    host_tags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Cover the hosts carrying one of these tags; empty covers every host of its site.",
    )
    site_id = serializers.CharField(required=False, allow_null=True)


class StacksRegistryAuthSerializer(serializers.Serializer):
    """The agent's docker-config triple for the registry it asked about."""

    username = serializers.CharField()
    password = serializers.CharField()
    serveraddress = serializers.CharField(help_text="The registry asked for, lowercased.")
