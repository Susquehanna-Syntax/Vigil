from rest_framework import serializers

from .models import Host, HostInventory


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


class HostInventorySerializer(serializers.ModelSerializer):
    hostname = serializers.CharField(source="host.hostname", read_only=True)
    host_id = serializers.UUIDField(source="host.id", read_only=True)
    os = serializers.CharField(source="host.os", read_only=True)
    ip_address = serializers.IPAddressField(source="host.ip_address", read_only=True)
    last_checkin = serializers.DateTimeField(source="host.last_checkin", read_only=True)
    status = serializers.CharField(source="host.status", read_only=True)
    mode = serializers.CharField(source="host.mode", read_only=True)
    tags = serializers.JSONField(source="host.tags", read_only=True)

    class Meta:
        model = HostInventory
        fields = [
            "host_id",
            "hostname",
            "os",
            "ip_address",
            "status",
            "mode",
            "tags",
            "last_checkin",
            "mac_addresses",
            "ram_total_bytes",
            "cpu_model",
            "cpu_cores",
            "service_tag",
            "manufacturer",
            "model_name",
            "os_name",
            "os_version",
            "kernel_version",
            "architecture",
            "uptime_seconds",
            "last_logged_user",
            "bios_version",
            "bios_date",
            "system_timezone",
            "disks",
            "custom_columns",
            "updated_at",
        ]
        read_only_fields = fields