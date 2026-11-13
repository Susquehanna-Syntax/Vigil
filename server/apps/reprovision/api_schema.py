"""OpenAPI shapes of the reprovisioning endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class ReprovisionErrorSerializer(serializers.Serializer):
    """The hand-validated refusal every view in this app speaks: ``error``
    with one sentence the operator can act on. The two refusals built by DRF
    rather than by hand — a permission denial, a missing row — answer with
    ``detail``, which is why both keys are optional here and the shape covers
    every 4xx these views return."""

    error = serializers.CharField(required=False)
    detail = serializers.CharField(required=False)
    image_id = serializers.CharField(required=False)
    code = serializers.CharField(required=False)


class ReprovisionFieldErrorsSerializer(serializers.Serializer):
    """DRF's per-field shape, which ``is_valid(raise_exception=True)`` returns
    when a body fails ``OSImageSerializer`` or ``InstallProfileSerializer``;
    every other refusal in this app is one flat sentence."""

    non_field_errors = serializers.ListField(child=serializers.CharField(),
                                             required=False)


class ReprovisionCatalogEntrySerializer(serializers.Serializer):
    """One ``CATALOG`` row: static data, and the shape never varies."""

    id = serializers.CharField()
    name = serializers.CharField()
    os_family = serializers.CharField()
    version = serializers.CharField()
    architecture = serializers.CharField()
    url = serializers.URLField()
    sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()


class ReprovisionImageSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    os_family = serializers.CharField()
    version = serializers.CharField()
    architecture = serializers.CharField()
    sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()
    status = serializers.CharField()
    import_error = serializers.CharField(allow_blank=True)
    source_url = serializers.CharField(allow_blank=True)
    bytes_downloaded = serializers.IntegerField()
    created_at = serializers.DateTimeField()


class ReprovisionImageWriteSerializer(serializers.Serializer):
    """What ``image_list``'s POST branch reads: the whole registration body."""

    name = serializers.CharField()
    os_family = serializers.CharField()
    version = serializers.CharField()
    architecture = serializers.CharField(required=False)
    sha256 = serializers.CharField()


class ReprovisionImagePullSerializer(serializers.Serializer):
    """Either a catalog id or the custom-URL set; never mixed, and the
    catalog's digest wins when both arrive."""

    catalog_id = serializers.CharField(required=False)
    url = serializers.CharField(required=False)
    sha256 = serializers.CharField(required=False)
    os_family = serializers.CharField(required=False)
    name = serializers.CharField(required=False)
    version = serializers.CharField(required=False)
    architecture = serializers.CharField(required=False)


class ReprovisionImageUploadSerializer(serializers.Serializer):
    """Multipart. The ``iso`` file part is a binary upload field, which the
    OpenAPI document declares separately from these body fields."""

    name = serializers.CharField()
    os_family = serializers.CharField()
    version = serializers.CharField(required=False)
    architecture = serializers.CharField(required=False)
    sha256 = serializers.CharField()


class ReprovisionProfileSerializer(serializers.Serializer):
    """The body ``InstallProfileSerializer`` renders. It is a
    ``ModelSerializer`` with an explicit field list (minus the encrypted
    password), so these are its fields and their types; ``admin_password``
    is write-only and never comes back."""

    id = serializers.CharField()
    site_id = serializers.CharField(allow_null=True)
    name = serializers.CharField()
    image = serializers.CharField()
    disk_target = serializers.CharField()
    partition_scheme = serializers.CharField()
    filesystem = serializers.CharField()
    network_mode = serializers.CharField()
    static_address = serializers.CharField(allow_blank=True)
    gateway = serializers.CharField(allow_blank=True)
    dns = serializers.CharField(allow_blank=True)
    timezone = serializers.CharField()
    locale = serializers.CharField()
    keyboard = serializers.CharField()
    admin_username = serializers.CharField()
    ssh_authorized_keys = serializers.CharField(allow_blank=True)
    extra_packages = serializers.JSONField()
    raw_append = serializers.CharField(allow_blank=True)
    completion_tags = serializers.JSONField()
    #: The row-backed tag mirror. M2M-backed, so DRF omits it while the
    #: relation is empty: a profile whose ``completion_tags`` are set has
    #: rows from its first save, one built straight in the database does not
    #: until the string field is next written.
    completion_tag_rows = serializers.ListField(child=serializers.CharField(),
                                                required=False)
    agent_profile = serializers.CharField(allow_null=True)
    deadline_minutes = serializers.IntegerField()
    created_at = serializers.DateTimeField()


class ReprovisionProfileCreateSerializer(serializers.Serializer):
    """Required fields follow the model; blank-allowed ones may be omitted,
    the model defaults filling in."""

    name = serializers.CharField()
    image = serializers.CharField()
    disk_target = serializers.CharField()
    admin_username = serializers.CharField(required=False)
    admin_password = serializers.CharField(required=False, allow_blank=True,
                                           help_text="Crypt hash, never plaintext.")
    ssh_authorized_keys = serializers.CharField(required=False, allow_blank=True)
    site_id = serializers.CharField(required=False, allow_null=True,
                                    help_text="Set by scoping on a write; ignored from the body.")
    agent_profile = serializers.CharField(required=False, allow_null=True)
    partition_scheme = serializers.CharField(required=False)
    filesystem = serializers.CharField(required=False)
    network_mode = serializers.CharField(required=False)
    static_address = serializers.CharField(required=False, allow_blank=True)
    gateway = serializers.CharField(required=False, allow_blank=True)
    dns = serializers.CharField(required=False, allow_blank=True)
    timezone = serializers.CharField(required=False)
    locale = serializers.CharField(required=False)
    keyboard = serializers.CharField(required=False)
    extra_packages = serializers.JSONField(required=False)
    raw_append = serializers.CharField(required=False, allow_blank=True)
    completion_tags = serializers.JSONField(required=False)
    deadline_minutes = serializers.IntegerField(required=False)


class ReprovisionProfilePatchSerializer(serializers.Serializer):
    """Every field of :class:`ReprovisionProfileCreateSerializer` a PATCH
    reads, each optional; a blank ``admin_password`` keeps the stored hash."""

    name = serializers.CharField(required=False)
    image = serializers.CharField(required=False)
    disk_target = serializers.CharField(required=False)
    admin_username = serializers.CharField(required=False)
    admin_password = serializers.CharField(required=False, allow_blank=True,
                                           help_text="Crypt hash, never plaintext.")
    ssh_authorized_keys = serializers.CharField(required=False, allow_blank=True)
    agent_profile = serializers.CharField(required=False, allow_null=True)
    partition_scheme = serializers.CharField(required=False)
    filesystem = serializers.CharField(required=False)
    network_mode = serializers.CharField(required=False)
    static_address = serializers.CharField(required=False, allow_blank=True)
    gateway = serializers.CharField(required=False, allow_blank=True)
    dns = serializers.CharField(required=False, allow_blank=True)
    timezone = serializers.CharField(required=False)
    locale = serializers.CharField(required=False)
    keyboard = serializers.CharField(required=False)
    extra_packages = serializers.JSONField(required=False)
    raw_append = serializers.CharField(required=False, allow_blank=True)
    completion_tags = serializers.JSONField(required=False)
    deadline_minutes = serializers.IntegerField(required=False)


class ReprovisionPreviewSerializer(serializers.Serializer):
    """``{filename: rendered text}``. Ubuntu emits two files, the others one,
    so the keys are per-family data rather than a fixed schema."""

    files = serializers.DictField(child=serializers.CharField())


class ReprovisionProfilePreviewSerializer(serializers.Serializer):
    host = serializers.CharField()


class ReprovisionPreflightSerializer(serializers.Serializer):
    """202 when no probe has completed yet. The 200 is the agent's §5
    snapshot, free-form by design, so the view maps it to a plain object."""

    dispatched = serializers.BooleanField()


class ReprovisionPreflightRequestSerializer(serializers.Serializer):
    host = serializers.CharField()
    disk_target = serializers.CharField(required=False)
    os_family = serializers.CharField(required=False)


class ReprovisionJobSerializer(serializers.Serializer):
    id = serializers.CharField()
    host = serializers.CharField()
    hostname = serializers.CharField()
    image = serializers.CharField()
    image_name = serializers.CharField()
    profile = serializers.CharField()
    profile_name = serializers.CharField()
    state = serializers.CharField()
    state_changed_at = serializers.DateTimeField()
    requested_at = serializers.DateTimeField()
    deadline = serializers.DateTimeField()
    failure_reason = serializers.CharField(allow_blank=True)
    completion_tag = serializers.CharField(allow_blank=True)
    preflight = serializers.JSONField()
    answer_fetched_at = serializers.DateTimeField(allow_null=True)
    post_playbook = serializers.CharField(allow_null=True)
    post_run = serializers.CharField(allow_null=True)


class ReprovisionJobCreateSerializer(serializers.Serializer):
    """The ceremony (§4.4): password AND TOTP AND the typed hostname."""

    host = serializers.CharField()
    image = serializers.CharField()
    profile = serializers.CharField()
    password = serializers.CharField(write_only=True)
    totp = serializers.CharField(required=False, write_only=True)
    typed_hostname = serializers.CharField(required=False)
    acknowledge_plaintext_transport = serializers.BooleanField(required=False)


class ReprovisionEnrollSerializer(serializers.Serializer):
    id = serializers.CharField()
    status = serializers.CharField()


class ReprovisionEnrollRequestSerializer(serializers.Serializer):
    enroll_token = serializers.CharField()
    agent_token = serializers.CharField()
