from rest_framework import serializers

from .models import InstallProfile, OSImage, RebuildJob


class OSImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = OSImage
        fields = ["id", "name", "os_family", "version", "architecture",
                  "sha256", "size_bytes", "status", "import_error",
                  "source_url", "bytes_downloaded", "created_at"]
        read_only_fields = ["status", "import_error", "size_bytes",
                            "source_url", "bytes_downloaded", "created_at"]


class InstallProfileSerializer(serializers.ModelSerializer):
    """admin_password_encrypted is excluded on purpose — the API never hands
    back a stored secret, not even to an admin."""

    admin_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        help_text="Crypt hash (e.g. from mkpasswd -m sha-512), never plaintext.")

    class Meta:
        model = InstallProfile
        exclude = ["admin_password_encrypted", "created_by"]

    def validate(self, attrs):
        """A profile with neither a password hash nor an SSH key produces a
        machine nobody can log into. Refuse it here rather than discover it
        after the disk is wiped."""
        keys = attrs.get("ssh_authorized_keys", getattr(
            self.instance, "ssh_authorized_keys", "")) or ""
        password = attrs.get("admin_password") or ""
        if self.instance is not None and not password:
            from apps.hosts.crypto import decrypt_secret
            password = decrypt_secret(self.instance.admin_password_encrypted)
        if not keys.strip() and not password.strip():
            raise serializers.ValidationError(
                "Set an SSH key or an admin password hash — otherwise the "
                "rebuilt machine has no way for anyone to log in.")

        if attrs.get("network_mode") == InstallProfile.NetworkMode.STATIC:
            if not attrs.get("static_address") or not attrs.get("gateway"):
                raise serializers.ValidationError(
                    "Static networking needs both static_address and gateway.")
        return attrs

    def _store_password(self, instance, password):
        from apps.hosts.crypto import encrypt_secret

        instance.admin_password_encrypted = encrypt_secret(password)
        instance.save(update_fields=["admin_password_encrypted"])

    def create(self, validated_data):
        password = validated_data.pop("admin_password", "")
        instance = super().create(validated_data)
        if password:
            self._store_password(instance, password)
        return instance

    def update(self, instance, validated_data):
        password = validated_data.pop("admin_password", None)
        instance = super().update(instance, validated_data)
        if password:
            self._store_password(instance, password)
        return instance


class RebuildJobSerializer(serializers.ModelSerializer):
    hostname = serializers.CharField(source="host.hostname", read_only=True)
    image_name = serializers.CharField(source="image.name", read_only=True)
    profile_name = serializers.CharField(source="profile.name", read_only=True)

    class Meta:
        model = RebuildJob
        fields = ["id", "host", "hostname", "image", "image_name", "profile",
                  "profile_name", "state", "state_changed_at", "requested_at",
                  "deadline", "failure_reason", "completion_tag", "preflight",
                  "answer_fetched_at", "post_baseline", "post_run"]
        read_only_fields = fields
