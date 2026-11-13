"""OpenAPI shapes of the core host endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class HostsHostSerializer(serializers.Serializer):
    """One host as ``HostSerializer`` renders it.

    ``site``/``site_name`` come from the serializer context: a read with no
    context, or a Community build without the sites app, reports null and "".
    """

    windows_updates = serializers.DictField(
        allow_null=True,
        help_text="The agent's last count of what is missing — pending, critical, "
                  "important, reboot_required. Null means it never counted, which "
                  "is not the same as zero.")
    windows_updates_at = serializers.DateTimeField(allow_null=True)
    container_engines = serializers.ListField(
        child=serializers.DictField(), allow_null=True,
        help_text="Engines the agent found; null until an agent reports.")
    id = serializers.CharField()
    hostname = serializers.CharField()
    os = serializers.CharField()
    kernel = serializers.CharField()
    ip_address = serializers.CharField(allow_null=True)
    status = serializers.CharField(help_text="pending, online, offline or rejected.")
    mode = serializers.CharField(help_text="monitor, managed or full_control.")
    tags = serializers.ListField(child=serializers.CharField())
    agent_version = serializers.CharField()
    agent_allowlist = serializers.ListField(
        child=serializers.CharField(), allow_null=True,
        help_text="The actions the agent will run, as it last reported them. "
                  "Null is unknown — not the same as refusing.")
    agent_allow_reprovision = serializers.BooleanField()
    agent_runs_as_root = serializers.BooleanField(
        allow_null=True, help_text="Null until the agent reports it.")
    reboot_required = serializers.BooleanField()
    last_checkin = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
    site = serializers.CharField(allow_null=True, help_text="Owning site id.")
    site_name = serializers.CharField()


class HostsDeletedSerializer(serializers.Serializer):
    deleted = serializers.CharField(help_text="The hostname that was removed.")


class HostsDeviceSerializer(serializers.Serializer):
    """One unmanaged device as ``UnmanagedDeviceSerializer`` renders it."""

    id = serializers.CharField()
    name = serializers.CharField()
    device_type = serializers.CharField()
    device_type_label = serializers.CharField(
        help_text="The human name of the type, e.g. 'Access Point'.")
    ip_address = serializers.CharField(allow_null=True)
    mac_address = serializers.CharField()
    vendor = serializers.CharField()
    location = serializers.CharField()
    serial = serializers.CharField()
    warranty_end = serializers.DateField(allow_null=True)
    owner = serializers.CharField()
    extra = serializers.DictField(child=serializers.CharField(),
                                  help_text='Free-form facts, as {"Rack": "B4"}.')
    notes = serializers.CharField()
    site_id = serializers.CharField(
        allow_null=True,
        help_text="The site the device is filed in; set by the view from the "
                  "caller's write scope, never taken from a PATCH body.")
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class HostsDeviceWriteSerializer(serializers.Serializer):
    """The body ``UnmanagedDeviceSerializer`` validates on create and PATCH.

    Every field is optional here because a PATCH of one key is valid; ``name``
    is still required to be non-blank whenever it is sent, and an omitted
    ``device_type`` becomes ``other``.
    """

    name = serializers.CharField(required=False)
    device_type = serializers.CharField(required=False)
    ip_address = serializers.CharField(required=False, allow_null=True)
    mac_address = serializers.CharField(required=False)
    vendor = serializers.CharField(required=False)
    location = serializers.CharField(required=False)
    serial = serializers.CharField(required=False)
    warranty_end = serializers.DateField(required=False, allow_null=True)
    owner = serializers.CharField(required=False)
    extra = serializers.DictField(child=serializers.CharField(), required=False,
                                  help_text="At most 50 named facts, each name "
                                            "60 characters and each value 300.")
    notes = serializers.CharField(required=False)


class HostsValidationErrorSerializer(serializers.Serializer):
    """DRF's field errors: a key per rejected field with its messages.

    A device body is refused before anything else runs, and the keys are the
    names of whichever fields went wrong, so this declares no fields at all
    rather than declaring a wrong one. ``shape_errors`` then treats it as
    free-form, which is exactly what the body is.
    """

    def get_fields(self):
        return {}


class HostsAdConfigSerializer(serializers.Serializer):
    """The AD import settings. The bind password is never returned — only
    whether one is stored."""

    ldap_url = serializers.CharField()
    bind_dn = serializers.CharField()
    base_dn = serializers.CharField()
    computer_ou = serializers.CharField()
    enabled = serializers.BooleanField()
    last_sync = serializers.DateTimeField(allow_null=True)
    last_sync_status = serializers.CharField()
    has_password = serializers.BooleanField()


class HostsAdConfigWriteSerializer(serializers.Serializer):
    """The upsert body. A field absent from it is stored as ""; the settings
    row itself is created on the first POST."""

    ldap_url = serializers.CharField(required=False, help_text="Kept to 512 characters.")
    bind_dn = serializers.CharField(required=False, help_text="Kept to 512 characters.")
    base_dn = serializers.CharField(required=False, help_text="Kept to 512 characters.")
    computer_ou = serializers.CharField(required=False, help_text="Kept to 512 characters.")
    enabled = serializers.BooleanField(
        required=False, help_text="Absent means False: the import runs only when set.")
    #: An empty or null password leaves the stored one alone; anything else
    #: replaces it, encrypted at rest.
    bind_password = serializers.CharField(required=False, allow_null=True)


class HostsAdConfigSavedSerializer(serializers.Serializer):
    ok = serializers.BooleanField()


class HostsAdSyncQueuedSerializer(serializers.Serializer):
    """The Celery path: the import runs in a worker."""

    queued = serializers.BooleanField()
    task_id = serializers.CharField()


class HostsAdSyncInlineSerializer(serializers.Serializer):
    """No broker: the import ran in the request."""

    queued = serializers.BooleanField()
    #: ``import_ad_computers``' summary — created, updated, errors, and an
    #: ``error`` key naming why when the run was refused outright.
    result = serializers.DictField()


class HostsTagsWriteSerializer(serializers.Serializer):
    """The complete replacement tag set; a tag absent from it is removed."""

    #: Not a validated list of strings here: the view refuses a body that is
    #: not a list with its own 400, and drops anything else per entry.
    tags = serializers.ListField(child=serializers.CharField())


class HostsTotpSerializer(serializers.Serializer):
    """The second factor the gate consumes; nothing else in the body is read."""

    totp = serializers.CharField(
        required=False,
        help_text="The current 6-digit code, or a one-time `wa_…` key token "
                  "from a WebAuthn assertion.")


class HostsTaskQueuedSerializer(serializers.Serializer):
    status = serializers.CharField(help_text='"queued" or "already_queued".')
    task_id = serializers.CharField()


class HostsErrorSerializer(serializers.Serializer):
    """The hand-built refusal these views speak: ``error`` with one sentence.

    DRF's own denials answer with ``detail`` instead, so both keys are here and
    each is optional — this is the shape of every 4xx in these views.
    """

    error = serializers.CharField(required=False)
    detail = serializers.CharField(required=False)


class HostsTaskSerializer(serializers.Serializer):
    """A task handed to an agent at check-in. ``sig_v`` appears only for an
    agent that advertised the ``signed_v2`` feature; ``created_at`` is kept for
    agents too old for ``dispatched_at``, which is what a current agent times
    its TTL from."""

    id = serializers.CharField()
    host_id = serializers.CharField()
    action = serializers.CharField()
    params = serializers.DictField()
    nonce = serializers.CharField()
    signature = serializers.CharField()
    sig_v = serializers.IntegerField(required=False)
    ttl_seconds = serializers.IntegerField()
    dispatched_at = serializers.DateTimeField()
    created_at = serializers.DateTimeField()


class HostsKeyRotationSerializer(serializers.Serializer):
    """The next signing key and the active key's signature introducing it —
    present in a check-in response only while a rotation is under way."""

    next = serializers.CharField()
    sig = serializers.CharField()


class HostsCheckinSerializer(serializers.Serializer):
    """What an approved agent gets back: the task-signing key, its tasks, the
    signed fallback relay list, and the digest of the software list the server
    holds (empty when it holds none)."""

    status = serializers.CharField()
    public_key = serializers.CharField()
    key_rotation = HostsKeyRotationSerializer(required=False)
    tasks = HostsTaskSerializer(many=True)
    relays = serializers.ListField(child=serializers.CharField(),
                                   help_text="Fallback relay URLs, in priority order.")
    direct = serializers.BooleanField(help_text="Whether the dashboard itself still takes agents.")
    relays_issued = serializers.IntegerField()
    relays_sig = serializers.CharField()
    software_digest = serializers.CharField(allow_blank=True)


class HostsCheckinBodySerializer(serializers.Serializer):
    """The check-in body. Every field is optional — an agent too old to report
    one simply omits it, and the server keeps what it already knew."""

    hostname = serializers.CharField(required=False)
    os = serializers.CharField(required=False)
    kernel = serializers.CharField(required=False)
    machine_id = serializers.CharField(required=False)
    mode = serializers.CharField(required=False)
    vigil_version = serializers.CharField(required=False)
    key_ack = serializers.CharField(required=False)
    allowlist = serializers.ListField(child=serializers.CharField(), required=False)
    allow_reprovision = serializers.BooleanField(required=False)
    allow_remote_shell = serializers.BooleanField(required=False)
    allow_remote_desktop = serializers.BooleanField(required=False)
    runs_as_root = serializers.BooleanField(required=False)
    features = serializers.ListField(child=serializers.CharField(), required=False)
    reboot_required = serializers.BooleanField(required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False,
                                 help_text="Namespaced under agent:* on the way in.")
    inventory = serializers.DictField(required=False)
    software = serializers.DictField(required=False)
    windows_updates = serializers.DictField(required=False)
    windows_update_list = serializers.DictField(required=False)
    container_engines = serializers.ListField(child=serializers.DictField(), required=False)
    docker_containers = serializers.ListField(child=serializers.DictField(), required=False)
    docker_resources = serializers.DictField(required=False)
    network = serializers.DictField(required=False)
    metrics = serializers.ListField(child=serializers.DictField(), required=False)


class HostsRegisterSerializer(serializers.Serializer):
    """An agent enrolling itself. Tags seed the host's own tag list and land
    under ``agent:*``; everything else becomes a field of the new Host."""

    agent_token = serializers.CharField(help_text="16-128 letters, digits, '-' or '_'.")
    hostname = serializers.CharField()
    machine_id = serializers.CharField(required=False)
    os = serializers.CharField(required=False)
    kernel = serializers.CharField(required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False)


class HostsRegisterResponseSerializer(serializers.Serializer):
    """The host's id and its status, which is ``pending`` until an admin
    approves it — an already-known token answers 200 with the same shape."""

    id = serializers.CharField()
    status = serializers.CharField()


class HostsTransportAckSerializer(serializers.Serializer):
    acknowledged = serializers.BooleanField()
    acknowledged_by = serializers.CharField(allow_blank=True,
                                            help_text="Empty while nobody stands behind it.")
    acknowledged_at = serializers.DateTimeField(allow_null=True)
    request_is_secure = serializers.BooleanField(
        help_text="What the caller is talking over right now.")


class HostsTransportAckBodySerializer(serializers.Serializer):
    acknowledged = serializers.BooleanField(
        help_text="true records the acknowledgement as this admin's; false withdraws it.")


class HostsCheckPendingTokenSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="The agent token the wizard wrote into agent.yml.")


class HostsReplacementSerializer(serializers.Serializer):
    """The record this enrolment would move onto — the wizard's honest warning
    that the machine is already known."""

    id = serializers.CharField()
    hostname = serializers.CharField()
    #: The raw model value, not the DRF-rendered string: an agent that reads
    #: this gets a Python datetime in JSON, and JS coerces it either way.
    last_checkin = serializers.JSONField(allow_null=True)


class HostsCheckPendingSerializer(serializers.Serializer):
    """The enrollment wizard's poll. ``waiting`` answers only ``status``, so
    both other fields are optional."""

    status = serializers.CharField(
        help_text="``waiting`` until the host registers, then ``pending`` or ``approved``.")
    host = HostsHostSerializer(required=False,
                               help_text="Absent while the host has not registered yet.")
    replaces = HostsReplacementSerializer(allow_null=True, required=False)


class HostsInventoryRowSerializer(serializers.Serializer):
    """One inventory row. A host whose agent has never reported is still a
    row, with empty fields and ``updated_at`` null."""

    host_id = serializers.CharField()
    hostname = serializers.CharField()
    os = serializers.CharField(allow_blank=True)
    ip_address = serializers.IPAddressField(allow_null=True)
    status = serializers.CharField()
    mode = serializers.CharField()
    tags = serializers.JSONField()
    last_checkin = serializers.DateTimeField(allow_null=True)
    mac_addresses = serializers.DictField()
    ram_total_bytes = serializers.IntegerField(allow_null=True)
    cpu_model = serializers.CharField(allow_blank=True)
    cpu_cores = serializers.IntegerField(allow_null=True)
    service_tag = serializers.CharField(allow_blank=True)
    manufacturer = serializers.CharField(allow_blank=True)
    model_name = serializers.CharField(allow_blank=True)
    os_name = serializers.CharField(allow_blank=True)
    os_version = serializers.CharField(allow_blank=True)
    kernel_version = serializers.CharField(allow_blank=True)
    architecture = serializers.CharField(allow_blank=True)
    uptime_seconds = serializers.IntegerField(allow_null=True)
    last_logged_user = serializers.CharField(allow_blank=True)
    bios_version = serializers.CharField(allow_blank=True)
    bios_date = serializers.CharField(allow_blank=True)
    system_timezone = serializers.CharField(allow_blank=True)
    disks = serializers.ListField(child=serializers.DictField())
    custom_columns = serializers.DictField()
    updated_at = serializers.DateTimeField(allow_null=True)


class HostsInventoryListRowSerializer(HostsInventoryRowSerializer):
    """The row plus the container columns the Inventory page adds."""

    containers = serializers.IntegerField()
    containers_running = serializers.IntegerField()
    containers_outdated = serializers.IntegerField()
    stacks = serializers.ListField(child=serializers.DictField())


class HostsInventoryListSerializer(serializers.Serializer):
    rows = HostsInventoryListRowSerializer(many=True)
    custom_columns = serializers.ListField(
        child=serializers.CharField(),
        help_text="Every collect: column any row in scope carries.")


class HostsTagSerializer(serializers.Serializer):
    tag = serializers.CharField()
    host_count = serializers.IntegerField()


class HostsContainerSerializer(serializers.Serializer):
    """One container as an agent last reported it. ``rolled_back``,
    ``previous_image`` and ``outdated`` are computed per host by the view."""

    container_id = serializers.CharField(allow_blank=True)
    name = serializers.CharField()
    image = serializers.CharField(allow_blank=True)
    state = serializers.CharField(allow_blank=True)
    status = serializers.CharField(allow_blank=True)
    stack = serializers.CharField(allow_blank=True)
    service = serializers.CharField(allow_blank=True)
    cpu_percent = serializers.FloatField(allow_null=True)
    mem_usage_bytes = serializers.IntegerField(allow_null=True)
    mem_limit_bytes = serializers.IntegerField(allow_null=True)
    mem_percent = serializers.FloatField(allow_null=True)
    ports = serializers.ListField(child=serializers.DictField())
    image_id = serializers.CharField(allow_blank=True)
    image_digest = serializers.CharField(allow_blank=True)
    restart_policy = serializers.CharField(allow_blank=True)
    config_hash = serializers.CharField(allow_blank=True)
    rolled_back = serializers.CharField(allow_blank=True)
    previous_image = serializers.CharField(allow_blank=True)
    updated_at = serializers.DateTimeField(allow_null=True)
    host = serializers.CharField()
    host_hostname = serializers.CharField()
    outdated = serializers.BooleanField()


class HostsStackSerializer(serializers.Serializer):
    """A compose stack on one host (M11). ``seen_at`` is null for a managed
    stack whose containers have all gone."""

    id = serializers.IntegerField()
    project = serializers.CharField()
    ownership = serializers.CharField()
    config_files = serializers.ListField(child=serializers.CharField())
    working_dir = serializers.CharField(allow_blank=True)
    engine = serializers.CharField(allow_blank=True)
    container_count = serializers.IntegerField()
    running_count = serializers.IntegerField()
    seen_at = serializers.DateTimeField(allow_null=True)
