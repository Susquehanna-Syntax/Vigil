"""OpenAPI shapes of the instance settings endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class InstanceSettingFieldSerializer(serializers.Serializer):
    name = serializers.CharField()
    #: One of registry.py's kinds: str, secret, int, float, bool, timezone, choice.
    kind = serializers.CharField()
    label = serializers.CharField()
    help = serializers.CharField(allow_blank=True)
    placeholder = serializers.CharField(allow_blank=True)
    choices = serializers.ListField(child=serializers.CharField())
    #: True when the environment sets the variable; the field is read-only then.
    env_locked = serializers.BooleanField()
    #: Where the effective value came from: "env", "db" or "default".
    source = serializers.CharField()
    is_set = serializers.BooleanField()
    #: Always "" for a secret: the value never leaves the server.
    value = serializers.CharField(allow_blank=True)
    #: The code default. Only non-secret fields carry it, so the field is optional.
    default = serializers.CharField(allow_blank=True, required=False)


class InstanceSettingGroupSerializer(serializers.Serializer):
    id = serializers.CharField()
    label = serializers.CharField()
    help = serializers.CharField(allow_blank=True)
    #: True when a site admin reads the group but may not change it.
    read_only = serializers.BooleanField()
    fields = InstanceSettingFieldSerializer(many=True)


class InstanceSettingsSerializer(serializers.Serializer):
    groups = InstanceSettingGroupSerializer(many=True)


class InstanceSettingsPostSerializer(serializers.Serializer):
    values = serializers.DictField(
        child=serializers.CharField(required=False, allow_null=True),
        help_text="Setting name to new value. A secret sent empty is left alone.")
    clear = serializers.ListField(
        child=serializers.CharField(),
        help_text="Setting names to delete, reverting each to its default.")


class InstanceSettingsChangedSerializer(serializers.Serializer):
    changed = serializers.ListField(child=serializers.CharField())
    #: Names in this POST that the environment pins, so nothing was written.
    env_locked = serializers.ListField(child=serializers.CharField())


class InstanceDetailErrorsSerializer(serializers.Serializer):
    detail = serializers.CharField()
    #: Per-setting rejections, name to message; present when a value fails validation.
    errors = serializers.DictField(child=serializers.CharField(), required=False)


class IntegrationTestResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    detail = serializers.CharField()


class IntegrationTestSerializer(serializers.Serializer):
    target = serializers.ChoiceField(
        choices=["greenbone", "nessus", "jackil", "email"],
        help_text="Which integration to probe with the settings now in force.")


class InstanceDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class BackupRunSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    started_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    state = serializers.ChoiceField(choices=["running", "ok", "failed"])
    name = serializers.CharField(allow_blank=True)
    size_bytes = serializers.IntegerField(allow_null=True)
    error = serializers.CharField(allow_blank=True)
    #: "" when no off-site copy is configured, else "copied" or "failed".
    offsite = serializers.CharField(allow_blank=True)
    offsite_error = serializers.CharField(allow_blank=True)
