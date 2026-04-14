from rest_framework import serializers

from .models import VulnSummary


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
