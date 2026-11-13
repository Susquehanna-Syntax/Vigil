"""OpenAPI shapes of Vigil's own endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class VigilHealthSerializer(serializers.Serializer):
    status = serializers.CharField()


class VigilScannerSerializer(serializers.Serializer):
    name = serializers.CharField()
    configured = serializers.BooleanField()


class VigilAboutSerializer(serializers.Serializer):
    licensed_to = serializers.CharField(allow_null=True)
    license_id = serializers.CharField(allow_null=True)
    vigil_version = serializers.CharField()
    expected_agent_version = serializers.CharField(allow_blank=True)
    python_version = serializers.CharField()
    database = serializers.CharField()
    timezone = serializers.CharField()
    scanners = VigilScannerSerializer(many=True)
    edition = serializers.CharField()
    features = serializers.ListField(child=serializers.CharField())


class VigilHostStatusCountsSerializer(serializers.Serializer):
    pending = serializers.IntegerField()
    online = serializers.IntegerField()
    offline = serializers.IntegerField()
    rejected = serializers.IntegerField()


class VigilCheckinWindowSerializer(serializers.Serializer):
    """Hosts that checked in within each window. The keys start with a digit,
    so they are declared in ``get_fields`` under their real names: the schema
    names a property after the field, not its ``source``."""

    def get_fields(self):
        return {window: serializers.IntegerField() for window in ("1m", "5m", "1h")}


class VigilSigningKeySerializer(serializers.Serializer):
    active = serializers.CharField()
    next = serializers.CharField(allow_null=True)
    # Present only while a next key is on offer: how many of the recently
    # approved hosts have taken it.
    acknowledged = serializers.IntegerField(required=False)
    hosts = serializers.IntegerField(required=False)


class VigilInstanceHealthSerializer(serializers.Serializer):
    """``/metrics``' numbers plus the two rows the About page adds."""

    hosts = VigilHostStatusCountsSerializer()
    checked_in = VigilCheckinWindowSerializer()
    remote_sessions_open = serializers.IntegerField()
    tasks = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Task count by state. A state with no tasks is absent.")
    celery_broker_up = serializers.BooleanField()
    celery_queue_depth = serializers.IntegerField(allow_null=True)
    database_bytes = serializers.IntegerField(
        allow_null=True, help_text="PostgreSQL only; null on any other vendor.")
    signing_key = VigilSigningKeySerializer()


class VigilSiteChoiceSerializer(serializers.Serializer):
    """One site a create form's picker may offer."""

    id = serializers.CharField()
    name = serializers.CharField()


class VigilWritableSitesSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    sites = VigilSiteChoiceSerializer(many=True)

    def get_fields(self):
        # ``global`` is a Python keyword, so it cannot be a class attribute.
        fields = super().get_fields()
        fields["global"] = serializers.BooleanField(
            help_text="Whether creating without a site is allowed.")
        return fields


class VigilSwitchableSiteSerializer(VigilSiteChoiceSerializer):
    is_global = serializers.BooleanField()


class VigilSiteSelectionSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(
        help_text="False when there is nothing to choose between.")
    sites = VigilSwitchableSiteSerializer(many=True)
    selected = serializers.ListField(
        child=serializers.CharField(), help_text="Empty means every site.")


class VigilSiteSelectionPutSerializer(serializers.Serializer):
    """One of the two: ``all`` clears the ticks, otherwise tick ``selected``."""

    all = serializers.BooleanField(required=False)
    selected = serializers.ListField(child=serializers.CharField(), required=False)
