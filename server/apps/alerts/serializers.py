from rest_framework import serializers

from .models import Alert, AlertRule


class AlertRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertRule
        fields = ["id", "name", "category", "metric", "operator", "threshold", "severity"]
        read_only_fields = fields


class AlertSerializer(serializers.ModelSerializer):
    host_hostname = serializers.CharField(source="host.hostname", read_only=True)
    rule = AlertRuleSerializer(read_only=True)

    class Meta:
        model = Alert
        fields = [
            "id",
            "host",
            "host_hostname",
            "rule",
            "state",
            "severity",
            "message",
            "metric_value",
            "fired_at",
            "acknowledged_at",
            "resolved_at",
        ]
        read_only_fields = fields