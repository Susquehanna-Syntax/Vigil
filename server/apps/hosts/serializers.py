from rest_framework import serializers

from .models import DockerContainer, Host, HostInventory, UnmanagedDevice


class HostSerializer(serializers.ModelSerializer):
    # Supplied by the view through serializer context so the whole list costs
    # two queries rather than one per host. Absent context yields null/"",
    # which is also what an AGPL-only build (no Business app) returns.
    site = serializers.SerializerMethodField()
    site_name = serializers.SerializerMethodField()

    class Meta:
        model = Host
        fields = [
            "windows_updates",
            "windows_updates_at",
            "container_engines",
            "id",
            "hostname",
            "os",
            "kernel",
            "ip_address",
            "status",
            "mode",
            "tags",
            "agent_version",
            "agent_allowlist",
            "agent_allow_reprovision",
            "agent_runs_as_root",
            "reboot_required",
            "last_checkin",
            "created_at",
            "site",
            "site_name",
        ]
        read_only_fields = fields

    def _site(self, obj):
        return (self.context.get("host_sites") or {}).get(obj.id)

    def get_site(self, obj):
        s = self._site(obj)
        return str(s.id) if s is not None else None

    def get_site_name(self, obj):
        s = self._site(obj)
        return s.name if s is not None else ""


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
    outdated = serializers.SerializerMethodField()

    rolled_back = serializers.SerializerMethodField()
    previous_image = serializers.SerializerMethodField()

    def get_rolled_back(self, obj) -> str:
        return self.context.get("rolled_back", {}).get((obj.host_id, obj.name), "")

    def get_previous_image(self, obj) -> str:
        """The image this container ran before its last update — a digest when
        known, else the image id — or "" when there is nothing to go back to."""
        return self.context.get("previous", {}).get((obj.host_id, obj.name), "")

    def get_outdated(self, obj) -> bool:
        # True while the container's outdated-image alert is open. The view
        # passes the open set in context so a whole list costs one query.
        return (obj.host_id, obj.name) in self.context.get("outdated", set())

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
            "image_id",
            "image_digest",
            "restart_policy",
            "config_hash",
            "rolled_back",
            "previous_image",
            "updated_at",
            "host",
            "host_hostname",
            "outdated",
        ]
        read_only_fields = fields


class UnmanagedDeviceSerializer(serializers.ModelSerializer):
    device_type_label = serializers.CharField(
        source="get_device_type_display",
        read_only=True,
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
            "serial",
            "warranty_end",
            "owner",
            "extra",
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

    def validate_extra(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                "Extra facts must be a set of named facts, like \"Rack\": \"B4\".")
        if len(value) > 50:
            raise serializers.ValidationError(
                "Extra facts are limited to 50 — keep the important ones.")
        for key, val in value.items():
            if not isinstance(key, str) or not isinstance(val, str):
                raise serializers.ValidationError(
                    "Extra facts must be names and values written as text.")
            if len(key) > 60:
                raise serializers.ValidationError(
                    "An extra fact name must be 60 characters or fewer.")
            if len(val) > 300:
                raise serializers.ValidationError(
                    "An extra fact value must be 300 characters or fewer.")
        return value
