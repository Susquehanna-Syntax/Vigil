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
