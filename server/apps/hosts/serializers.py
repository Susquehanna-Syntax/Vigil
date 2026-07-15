from rest_framework import serializers

from .models import DockerContainer, Host, HostInventory, UnmanagedDevice


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
            "agent_version",
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


class DockerContainerSerializer(serializers.ModelSerializer):
    host_hostname = serializers.CharField(source="host.hostname", read_only=True)

    class Meta:
        model = DockerContainer
        fields = [
            "container_id",
            "name",
            "image",
            "state",
            "status",
            "stack",
            "service",
            "cpu_percent",
            "mem_usage_bytes",
            "mem_limit_bytes",
            "mem_percent",
            "ports",
            "updated_at",
            "host",
            "host_hostname",
        ]
        read_only_fields = fields

class UnmanagedDeviceSerializer(serializers.ModelSerializer):
    device_type_label = serializers.CharField(
        source="get_device_type_display", read_only=True,
    )

    class Meta:
        model = UnmanagedDevice
        fields = [
            "id",
            "name",
            "device_type",
            "device_type_label",
            "ip_address",
            "mac_address",
            "vendor",
            "location",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "device_type_label", "created_at", "updated_at"]

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value
