"""OpenAPI shapes of the agent distribution endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class AgentBinarySerializer(serializers.Serializer):
    platform = serializers.CharField()
    platform_label = serializers.CharField()
    version = serializers.CharField(allow_blank=True)
    #: Always empty for a bundled binary: the digest is on the download's
    #: X-Vigil-SHA256 header, not in this body.
    sha256 = serializers.CharField(allow_blank=True)
    source = serializers.ChoiceField(choices=["bundled", "uploaded"])


class AgentUploadSerializer(serializers.Serializer):
    version = serializers.CharField(
        required=False, allow_blank=True,
        help_text="A label for the build. The digest is computed server-side.")
    totp = serializers.CharField(help_text="A current 2FA code; empty is refused.")
    binary = serializers.FileField(help_text="The agent executable for this platform.")


class AgentUploadResultSerializer(serializers.Serializer):
    platform = serializers.CharField()
    version = serializers.CharField(allow_blank=True)
    sha256 = serializers.CharField()


class AgentErrorSerializer(serializers.Serializer):
    """What the refusals here say. This app spells the field ``error``."""

    error = serializers.CharField()
