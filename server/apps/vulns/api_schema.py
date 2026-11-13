"""OpenAPI shapes of the vulnerability endpoints (sprint M5).

Documentation only: ``vuln_list``, ``finding_list``, ``score_history`` and
``scan_list`` render through the ModelSerializers in ``serializers.py``, which
the views' ``@extend_schema`` names directly; this module supplies the types
for every response the views build by hand.
"""
from rest_framework import serializers


class VulnErrorResponse(serializers.Serializer):
    error = serializers.CharField()


class VulnErrorActiveScanSerializer(VulnErrorResponse):
    active_scan_id = serializers.CharField()


class VulnDetailResponse(serializers.Serializer):
    detail = serializers.CharField()


class VulnFleetWorstSerializer(serializers.Serializer):
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()
    score = serializers.IntegerField()


class VulnFleetScoreSerializer(serializers.Serializer):
    score = serializers.IntegerField()
    host_count = serializers.IntegerField()
    worst = VulnFleetWorstSerializer(allow_null=True)


class VulnScanCreateSerializer(serializers.Serializer):
    scanner = serializers.CharField(
        required=False, allow_blank=True,
        help_text="``nessus`` or ``greenbone``; omitted means the first "
                  "configured engine.")
    totp = serializers.CharField(required=False,
                                 help_text="Current TOTP code — a scan is gated like a deploy.")


class VulnDataSerializer(serializers.Serializer):
    osv_advisories = serializers.IntegerField()
    osv_ecosystems = serializers.ListField(child=serializers.CharField())
    kev_entries = serializers.IntegerField()
    epss_scores = serializers.IntegerField()


class VulnDataLoadSerializer(serializers.Serializer):
    loaded = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Rows loaded per store: ``osv``, ``kev``, ``epss``.")


class VulnFixGroupHostSerializer(serializers.Serializer):
    host_id = serializers.UUIDField()
    hostname = serializers.CharField()
    installed_version = serializers.CharField(allow_blank=True, allow_null=True)
    evidence = serializers.ListField(child=serializers.CharField())
    evidence_text = serializers.ListField(child=serializers.CharField())
    confidence = serializers.ChoiceField(choices=["urgent", "confirmed", "file", "reported"])


class VulnFixGroupFixSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["upgrade", "kb", "detection", "none"])
    label = serializers.CharField()
    definition_id = serializers.CharField(required=False)


class VulnFixGroupSerializer(serializers.Serializer):
    fix_key = serializers.CharField()
    title = serializers.CharField()
    package = serializers.CharField()
    installed_version = serializers.CharField(allow_blank=True, allow_null=True)
    fixed_version = serializers.CharField(allow_blank=True, allow_null=True)
    affected_path = serializers.CharField(allow_blank=True, allow_null=True)
    vendor_status = serializers.CharField(allow_blank=True, allow_null=True)
    severity = serializers.CharField()
    cves = serializers.ListField(child=serializers.CharField())
    finding_count = serializers.IntegerField()
    host_count = serializers.IntegerField()
    hosts = VulnFixGroupHostSerializer(many=True)
    due_date = serializers.DateField(allow_null=True)
    cvss = serializers.FloatField(allow_null=True)
    epss = serializers.FloatField(allow_null=True)
    kev = serializers.BooleanField()
    running = serializers.BooleanField()
    scanners = serializers.ListField(child=serializers.CharField())
    fix = VulnFixGroupFixSerializer()


class VulnFixGroupListSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    results = VulnFixGroupSerializer(many=True)


class VulnFixGroupDeploySerializer(serializers.Serializer):
    fix_key = serializers.CharField(help_text="The group's ``fix_key``.")


class VulnFixGroupDeployResultSerializer(serializers.Serializer):
    definition_id = serializers.UUIDField()
    host_ids = serializers.ListField(child=serializers.CharField())


class VulnAnvilImportSerializer(serializers.Serializer):
    record = serializers.DictField(
        required=False,
        help_text="The Anvil record (SARIF 2.1.0 + anvil/*) as a JSON object; "
                  "send it as the multipart ``record`` file instead of in the body.")
    host_id = serializers.UUIDField(
        required=False,
        help_text="Pin every finding to this host instead of matching by hostname.")


class VulnAnvilImportResultSerializer(serializers.Serializer):
    imported = serializers.IntegerField()
    false_positive = serializers.IntegerField()
    no_host = serializers.IntegerField()
