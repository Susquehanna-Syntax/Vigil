"""OpenAPI shapes of the remote-session endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class RemoteSessionSerializer(serializers.Serializer):
    """One session row as the list renders it. ``has_recording`` is only true
    for a caller allowed to know recordings exist at all.
    """

    id = serializers.CharField()
    host = serializers.CharField()
    username = serializers.CharField()
    # The three values of RemoteSession.Kind, as the view returns them. Not a
    # ChoiceField: another app's field named ``kind`` already owns that enum
    # name, and renaming it is not this phase's to do.
    kind = serializers.CharField()
    container = serializers.CharField(allow_blank=True)
    state = serializers.ChoiceField(choices=["pending", "open", "closed",
                                             "refused", "expired"])
    created_at = serializers.DateTimeField(allow_null=True)
    opened_at = serializers.DateTimeField(allow_null=True)
    closed_at = serializers.DateTimeField(allow_null=True)
    close_reason = serializers.CharField(allow_blank=True)
    bytes_in = serializers.IntegerField()
    bytes_out = serializers.IntegerField()
    has_recording = serializers.BooleanField()


class RemoteSessionListSerializer(serializers.Serializer):
    """The newest fifty sessions on the hosts the caller can see."""

    sessions = RemoteSessionSerializer(many=True)


class RemoteSessionStartSerializer(serializers.Serializer):
    host = serializers.CharField(help_text="The host id to open a session on.")
    kind = serializers.CharField()
    container = serializers.CharField(required=False, allow_blank=True,
                                      help_text="Container name; container kind only.")
    totp = serializers.CharField(help_text="A current 2FA code; required by every kind.")


class RemoteSessionCreatedSerializer(serializers.Serializer):
    """What a start returns: the id, and the websocket to attach to."""

    id = serializers.CharField()
    ws = serializers.CharField()
    kind = serializers.CharField()
    host = serializers.CharField()
    #: Whether this session's output is being recorded; false when the license
    #: has no audit log, and never true for RDP (guacd records that one).
    recorded = serializers.BooleanField()


class RemoteSessionOptionSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    reason = serializers.CharField(allow_blank=True,
                                   help_text="Why not; empty when available.")


class RemoteSessionOptionsSerializer(serializers.Serializer):
    """What the Connect menu may offer for one host."""

    host = serializers.CharField()
    is_windows = serializers.BooleanField()
    os_family = serializers.ChoiceField(choices=["windows", "macos", "linux"])
    ssh_target = serializers.CharField()
    recorded = serializers.BooleanField()
    shell = RemoteSessionOptionSerializer()
    container = RemoteSessionOptionSerializer()
    rdp = RemoteSessionOptionSerializer()


class RemoteSessionCloseSerializer(serializers.Serializer):
    """202 and the reason the audit row will carry."""

    status = serializers.CharField()


class RemoteSessionUpgradeSerializer(serializers.Serializer):
    """What a feature the license does not cover answers with: 402 here.

    The body is vigil.licensing.upgrade_body's, which every app that gates on a
    feature returns verbatim; this app spells its refusals ``error`` (see
    ``RemoteSessionErrorSerializer``).
    """

    feature = serializers.CharField()
    licensed = serializers.BooleanField()
    detail = serializers.CharField()
    upgrade_url = serializers.URLField()


class RemoteSessionErrorSerializer(serializers.Serializer):
    """What the refusals here say. This app spells the field ``error``."""

    error = serializers.CharField()
