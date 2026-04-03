from rest_framework import serializers

from .models import Host


class HostSerializer(serializers.ModelSerializer):
    class Meta:
        model = Host
        fields = [
            "id",
            "hostname",
            "os",
            "kernel",
            "ip_address",
            "status",
            "mode",
            "tags",
            "last_checkin",
            "created_at",
        ]
        read_only_fields = fields