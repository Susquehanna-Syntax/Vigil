from rest_framework import serializers

from .models import VulnScan, VulnSummary


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
            "last_scan_at",
            "scanner_scan_id",
            "synced_at",
        ]
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
            "state",
            "nessus_scan_id",
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
