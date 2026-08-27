from rest_framework import serializers

from .models import VulnException, VulnFinding, VulnScan, VulnScoreHistory, VulnSummary


class VulnSummarySerializer(serializers.ModelSerializer):
    host_hostname = serializers.CharField(source="host.hostname", read_only=True)
    host_ip = serializers.CharField(source="host.ip_address", read_only=True, default=None)

    class Meta:
        model = VulnSummary
        fields = [
            "host",
            "host_hostname",
            "host_ip",
            "critical",
            "high",
            "medium",
            "low",
            "info",
            "overdue_count",
            "due_soon_count",
            "score",
            "last_scan_at",
            "scanner_scan_id",
            "synced_at",
        ]
        read_only_fields = fields


class VulnFindingExceptionSerializer(serializers.ModelSerializer):
    """The accepted-risk / deferred exception on a finding, if any."""

    class Meta:
        model = VulnException
        fields = ["kind", "reason", "expires_on"]
        read_only_fields = fields


class VulnFindingSerializer(serializers.ModelSerializer):
    host_hostname = serializers.CharField(source="host.hostname", read_only=True)
    days_remaining = serializers.IntegerField(read_only=True)
    overdue = serializers.BooleanField(read_only=True)
    exception = VulnFindingExceptionSerializer(read_only=True)

    class Meta:
        model = VulnFinding
        fields = [
            "id",
            "host",
            "host_hostname",
            "scanner",
            "plugin_id_or_oid",
            "cve_id",
            "title",
            "severity",
            "state",
            "package_name",
            "installed_version",
            "fixed_version",
            "first_seen",
            "last_seen",
            "resolved_at",
            "due_date",
            "days_remaining",
            "overdue",
            "exception",
        ]
        read_only_fields = fields


class VulnScoreHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = VulnScoreHistory
        fields = ["date", "score"]
        read_only_fields = fields


class VulnScanSerializer(serializers.ModelSerializer):
    host_hostname = serializers.CharField(source="host.hostname", read_only=True)
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True, default=None
    )

    class Meta:
        model = VulnScan
        fields = [
            "id",
            "host",
            "host_hostname",
            "scanner",
            "state",
            "external_scan_id",
            "target",
            "requested_at",
            "launched_at",
            "finished_at",
            "requested_by",
            "requested_by_username",
            "requested_via_task",
            "error",
        ]
        read_only_fields = fields
