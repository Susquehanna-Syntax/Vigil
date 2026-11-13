"""OpenAPI shapes of the relays endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class UpgradeRequiredSerializer(serializers.Serializer):
    """The 402 every license limit answers with, plus the two counts that make
    the limit concrete for the settings page.
    """

    feature = serializers.CharField()
    licensed = serializers.BooleanField()
    detail = serializers.CharField()
    upgrade_url = serializers.URLField()
    allowed = serializers.IntegerField()
    used = serializers.IntegerField()


class RelaySerializer(serializers.Serializer):
    """One relay. The link key is never among these fields; a create response
    adds it (see ``RelayCreatedSerializer``).
    """

    id = serializers.CharField()
    name = serializers.CharField()
    location = serializers.CharField(allow_blank=True)
    url = serializers.CharField(allow_blank=True, help_text="Empty for the built-in relay.")
    # The values rather than a ChoiceField: the same choice set under another
    # field name already owns the enum name this would take.
    kind = serializers.CharField()
    priority = serializers.IntegerField()
    enabled = serializers.BooleanField()
    #: Enabled but beyond the license's limit: configured, carrying nothing.
    over_limit = serializers.BooleanField()
    connected = serializers.BooleanField()
    last_seen_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField(allow_null=True)


class RelayCreateSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="1-80 characters.")
    url = serializers.CharField(help_text="https:// base URL agents use.")
    location = serializers.CharField(required=False, allow_blank=True)
    enabled = serializers.BooleanField(required=False,
                                       help_text="Default true; false waits idle.")
    priority = serializers.IntegerField(required=False,
                                        help_text="0-1000, default 100.")
    totp = serializers.CharField(help_text="A current 2FA code; empty is refused.")


class RelayCreatedSerializer(RelaySerializer):
    """The new relay, and with it the key: what create returns is the row plus
    ``key`` and ``key_sha256``, shown once and never again.
    """

    key = serializers.CharField()
    key_sha256 = serializers.CharField()


class RelayListSerializer(serializers.Serializer):
    allowed = serializers.IntegerField(help_text="What the license permits.")
    used = serializers.IntegerField(help_text="How many are in service now.")
    relays = RelaySerializer(many=True)


class RelayUpdateSerializer(serializers.Serializer):
    """Every field is optional; name and location need no 2FA, url and enabled
    do (and so does delete), priority never does.
    """

    name = serializers.CharField(required=False, help_text="1-80 characters.")
    location = serializers.CharField(required=False, allow_blank=True)
    url = serializers.CharField(required=False, help_text="Needs a 2FA code.")
    enabled = serializers.BooleanField(required=False, help_text="Needs a 2FA code.")
    priority = serializers.IntegerField(required=False, help_text="0-1000.")
    totp = serializers.CharField(required=False,
                                 help_text="A current 2FA code, required by the gated fields.")


class RelayRotateKeySerializer(serializers.Serializer):
    totp = serializers.CharField(help_text="A current 2FA code; empty is refused.")


class RelayKeySerializer(serializers.Serializer):
    """The key, shown exactly once — on create or rotate."""

    key = serializers.CharField()
    key_sha256 = serializers.CharField()


class RelayErrorSerializer(serializers.Serializer):
    """What the refusals here say. This app spells the field ``detail``."""

    detail = serializers.CharField()
