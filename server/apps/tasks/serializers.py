from rest_framework import serializers

from .models import Task


class TaskSerializer(serializers.ModelSerializer):
    host_hostname = serializers.CharField(source="host.hostname", read_only=True)
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True, default=None
    )

    class Meta:
        model = Task
        fields = [
            "id",
            "host",
            "host_hostname",
            "requested_by",
            "requested_by_username",
            "action",
            "params",
            "risk_level",
            "state",
            "nonce",
            "ttl_seconds",
            "result_output",
            "created_at",
            "dispatched_at",
            "completed_at",
        ]
        read_only_fields = fields