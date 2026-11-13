"""OpenAPI shapes of the software inventory endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class SoftwareDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class SoftwareErrorSerializer(serializers.Serializer):
    """What ``vigil.scoping.host_or_404`` answers for a host out of reach."""

    error = serializers.CharField()


class SoftwareAppPackageSerializer(serializers.Serializer):
    source = serializers.CharField()
    id = serializers.CharField()


class SoftwareAppRowSerializer(serializers.Serializer):
    name_key = serializers.CharField()
    name = serializers.CharField()
    sources = serializers.ListField(child=serializers.CharField())
    packages = SoftwareAppPackageSerializer(many=True)
    hosts = serializers.IntegerField()
    versions = serializers.DictField(child=serializers.IntegerField(),
                                     help_text="version → how many hosts have it")
    outdated = serializers.IntegerField()
    unmanaged = serializers.IntegerField()


class SoftwareAppListSerializer(serializers.Serializer):
    count = serializers.IntegerField(help_text="Every matching app, not just this page")
    results = SoftwareAppRowSerializer(many=True)


class SoftwareItemRowSerializer(serializers.Serializer):
    """The keys of ``_row`` in views.py. The host is named on the row only
    where one response covers several hosts."""

    name = serializers.CharField()
    source = serializers.CharField()
    package_id = serializers.CharField()
    version = serializers.CharField()
    latest_version = serializers.CharField()
    outdated = serializers.BooleanField()
    scope = serializers.CharField()
    user = serializers.CharField()
    managed = serializers.BooleanField()
    first_seen = serializers.DateTimeField()


class SoftwareAppItemRowSerializer(SoftwareItemRowSerializer):
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()


class SoftwareAppDetailSerializer(serializers.Serializer):
    name_key = serializers.CharField()
    rows = SoftwareAppItemRowSerializer(many=True)


class SoftwareHostItemSerializer(SoftwareItemRowSerializer):
    """``host_software``'s items: the host is in the URL, so not on the row."""

    publisher = serializers.CharField()


class SoftwareSnapshotSerializer(serializers.Serializer):
    digest = serializers.CharField()
    collected_at = serializers.DateTimeField(allow_null=True)
    received_at = serializers.DateTimeField()
    item_count = serializers.IntegerField()
    errors = serializers.DictField(child=serializers.CharField(),
                                   help_text="collector id → what went wrong")


class SoftwareHostSoftwareSerializer(serializers.Serializer):
    snapshot = SoftwareSnapshotSerializer(allow_null=True)
    items = SoftwareHostItemSerializer(many=True)


class SoftwareHostSummarySerializer(serializers.Serializer):
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()
    item_count = serializers.IntegerField()
    outdated = serializers.IntegerField()
    unmanaged = serializers.IntegerField()
    received_at = serializers.DateTimeField(
        allow_null=True, help_text="Null when the host never reported a snapshot")
    errors = serializers.DictField(child=serializers.CharField())


class SoftwareHostSummariesSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = SoftwareHostSummarySerializer(many=True)


class SoftwareUpdateRowSerializer(serializers.Serializer):
    kind = serializers.CharField()
    key = serializers.CharField()
    title = serializers.CharField(allow_blank=True)
    severity = serializers.CharField(allow_blank=True)
    classification = serializers.CharField(allow_blank=True)
    reboot_required = serializers.BooleanField()
    hosts = serializers.IntegerField()
    oldest_first_seen = serializers.DateTimeField()
    age_days = serializers.IntegerField()
    decision = serializers.CharField(help_text="approved, declined or undecided")
    decided_by = serializers.CharField(allow_null=True)


class SoftwareUpdateListSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = SoftwareUpdateRowSerializer(many=True)


class SoftwareUpdateHostSerializer(serializers.Serializer):
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()
    first_seen = serializers.DateTimeField()
    age_days = serializers.IntegerField()
    version = serializers.CharField(allow_blank=True)


class SoftwareUpdateHostsSerializer(serializers.Serializer):
    results = SoftwareUpdateHostSerializer(many=True)


class SoftwareUpdateDecideSerializer(serializers.Serializer):
    kind = serializers.CharField(help_text="windows or linux")
    keys = serializers.ListField(child=serializers.CharField(),
                                 help_text="1 to 300 characters each; at least one")
    decision = serializers.CharField(help_text="approved, declined or clear")


class SoftwareUpdateDecidedSerializer(serializers.Serializer):
    """The one key the response has is the decision that was made."""

    approved = serializers.IntegerField(required=False)
    declined = serializers.IntegerField(required=False)
    cleared = serializers.IntegerField(required=False)
