"""OpenAPI shapes of the notification channels API (sprint M5/M7).

Documentation only, like every app's api_schema.py: the views build their
responses by hand and these describe them.
"""
from rest_framework import serializers


class AlertChannelFieldSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()
    kind = serializers.ChoiceField(choices=["text", "url", "secret", "list", "choice"])
    required = serializers.BooleanField()
    help = serializers.CharField(allow_blank=True)
    choices = serializers.ListField(child=serializers.CharField())
    default = serializers.CharField(allow_blank=True)


class AlertChannelTypeSerializer(serializers.Serializer):
    kind = serializers.CharField()
    label = serializers.CharField()
    fields = AlertChannelFieldSerializer(many=True)
    feature = serializers.CharField(allow_blank=True, help_text="A license feature it needs, if any.")
    available = serializers.BooleanField()


class AlertChannelSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.CharField()
    kind_label = serializers.CharField()
    config = serializers.DictField(help_text="The type's settings, secrets left out.")
    secrets_set = serializers.ListField(child=serializers.CharField(),
                                        help_text="Secret settings that hold a value.")
    on_firing = serializers.BooleanField()
    on_resolved = serializers.BooleanField()
    enabled = serializers.BooleanField()
    site_id = serializers.UUIDField(allow_null=True)
    last_ok = serializers.BooleanField(allow_null=True)
    last_status = serializers.CharField(allow_blank=True)
    last_status_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()


class AlertChannelWriteSerializer(serializers.Serializer):
    name = serializers.CharField(required=False)
    kind = serializers.CharField(required=False, help_text="Required on create; cannot change.")
    config = serializers.DictField(required=False,
                                   help_text="A blank secret keeps the stored one.")
    site_id = serializers.UUIDField(required=False, allow_null=True)
    on_firing = serializers.BooleanField(required=False)
    on_resolved = serializers.BooleanField(required=False)
    enabled = serializers.BooleanField(required=False)


class AlertChannelTestSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    detail = serializers.CharField(allow_blank=True)
    channel = AlertChannelSerializer()
