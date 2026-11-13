"""OpenAPI shapes of the agent profile endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class AgentProfileFlagsSerializer(serializers.Serializer):
    """Everything except the name and the site. The wizard's form body is
    exactly this plus a ``site_id``, and the one-off config POST sends it as
    its ``overrides``."""

    mode = serializers.CharField(
        required=False,
        help_text='"monitor", "managed", or "full_control"; default monitor.')
    #: Action names; managed mode only. Sorted and de-duplicated on the way in.
    allowlist = serializers.ListField(child=serializers.CharField(),
                                      required=False)
    #: Null means "this dashboard": the agent talks to Vigil directly.
    primary_relay = serializers.CharField(required=False, allow_null=True,
                                          help_text="A relay id.")
    #: Ordered relay ids — the order is the data.
    fallback_relays = serializers.ListField(child=serializers.CharField(),
                                            required=False)
    include_dashboard_fallback = serializers.BooleanField(
        required=False, help_text="Add this dashboard as the last fallback.")
    allow_remote_shell = serializers.BooleanField(required=False)
    allow_remote_desktop = serializers.BooleanField(required=False)
    remote_shell_user = serializers.CharField(required=False)
    remote_desktop_port = serializers.IntegerField(required=False)
    allow_reprovision = serializers.BooleanField(required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    process_watch = serializers.ListField(child=serializers.CharField(),
                                          required=False)
    checkin_interval = serializers.IntegerField(
        required=False, help_text="Seconds, 10 to 30 days.")
    docker_check_interval = serializers.IntegerField(
        required=False, help_text="Seconds, 300 to 30 days.")
    software_interval = serializers.IntegerField(
        required=False, help_text="Seconds, 900 to 30 days.")


class AgentProfileSerializer(AgentProfileFlagsSerializer):
    """One profile as stored. Names are unique across the install; built-in
    profiles are the wizard's starting points and cannot be deleted.

    Every setting below carries a value whatever the request omitted, because
    ``_apply`` writes the whole validated record back: unlike an HTTP PATCH,
    absent fields here come back with their defaults rather than staying out.
    """

    id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    builtin = serializers.BooleanField()
    site_id = serializers.CharField(allow_null=True)
    #: Names of the install profiles that offer this one.
    install_profiles = serializers.ListField(child=serializers.CharField())
    created_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)


class AgentProfileWriteSerializer(AgentProfileFlagsSerializer):
    name = serializers.CharField(
        help_text="1-80 characters, unique across the install.")
    description = serializers.CharField(required=False,
                                        help_text="At most 300 characters.")
    site_id = serializers.CharField(required=False, allow_null=True)


class AgentActionSerializer(serializers.Serializer):
    """One action of the picker: what it is called and where the wizard lists it."""

    name = serializers.CharField()
    label = serializers.CharField()
    group = serializers.CharField(help_text='The wiki-style group, e.g. "service".')
    risk = serializers.CharField()
    #: False for an action no allowlist can ever grant.
    allowlistable = serializers.BooleanField()


class AgentEnrollConfigSerializer(serializers.Serializer):
    profile = serializers.CharField(
        required=False, allow_null=True,
        help_text="Profile id to render; omit or null for a one-off config.")
    overrides = serializers.DictField(
        required=False,
        help_text="Profile fields to change on the way to agent.yml. Unknown "
                  "keys are ignored rather than refused.")


class AgentEnrollConfigCreatedSerializer(serializers.Serializer):
    id = serializers.CharField()
    #: The raw token: this response is the only place it exists.
    token = serializers.CharField()
    expires_at = serializers.DateTimeField()
    server_url = serializers.CharField(
        help_text="Where the agent will check in — a relay, or this dashboard.")
    #: The rendered agent.yml keys, as ``render.agent_settings`` builds them:
    #: which of them appear depends on the mode and the settings.
    settings = serializers.DictField()
    #: What the server turned off while validating, and why.
    notes = serializers.ListField(child=serializers.CharField())


class AgentProfileErrorSerializer(serializers.Serializer):
    """What refusing to delete a built-in profile says."""

    error = serializers.CharField()


class AgentProfileDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class AgentProfileErrorsSerializer(serializers.Serializer):
    """Field-by-field refusals, field name to reason. The wizard reads this
    instead of the bare message, so every bad setting gets its own complaint."""

    errors = serializers.DictField(child=serializers.CharField())
