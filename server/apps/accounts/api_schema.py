"""OpenAPI shapes of the accounts endpoints (sprint M5): security keys,
sessions, TOTP and users.

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class AccountsErrorSerializer(serializers.Serializer):
    """The refusal every view in this app speaks: ``error`` with one sentence
    the operator can act on."""

    error = serializers.CharField()


class AccountsKeyRowSerializer(serializers.Serializer):
    """One security key as the settings page lists it. Never the credential id
    or the public key — those are the authenticator's identity."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    created_at = serializers.DateTimeField(allow_null=True)
    last_used_at = serializers.DateTimeField(allow_null=True)


class AccountsKeyCreatedSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class AccountsRegisterRequestSerializer(serializers.Serializer):
    """The register POST's body: a name, and the WebAuthn credential, whose
    shape is the browser's to define and the library's to verify."""

    name = serializers.CharField(required=False, allow_null=True)
    credential = serializers.DictField(required=False, allow_null=True)


class AccountsRegisterOptionsRequestSerializer(serializers.Serializer):
    #: Required only when the user already has a second factor; the view reads
    #: it through ``require_totp_confirmation``, which also takes a key token.
    totp = serializers.CharField(required=False, allow_blank=True)


class AccountsRpEntitySerializer(serializers.Serializer):
    name = serializers.CharField()
    id = serializers.CharField()


class AccountsRegisterUserSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="The user id, base64url-encoded.")
    name = serializers.CharField()
    displayName = serializers.CharField()


class AccountsPubKeyCredParamSerializer(serializers.Serializer):
    type = serializers.CharField()
    alg = serializers.IntegerField()


class AccountsAuthenticatorSelectionSerializer(serializers.Serializer):
    residentKey = serializers.CharField()
    requireResidentKey = serializers.BooleanField()
    userVerification = serializers.CharField()


class AccountsRegisterOptionsSerializer(serializers.Serializer):
    """The creation options the WebAuthn dialog is handed, verbatim from
    ``webauthn.registration_options``, plus our two additions: ``rpId`` (the
    JSON does not carry it) and ``origin``."""

    rp = AccountsRpEntitySerializer()
    user = AccountsRegisterUserSerializer()
    challenge = serializers.CharField()
    pubKeyCredParams = AccountsPubKeyCredParamSerializer(many=True)
    timeout = serializers.IntegerField(required=False)
    excludeCredentials = serializers.ListField(
        child=serializers.DictField(), required=False, allow_empty=True)
    authenticatorSelection = AccountsAuthenticatorSelectionSerializer()
    attestation = serializers.CharField()
    rpId = serializers.CharField()
    origin = serializers.CharField()


class AccountsAssertionCredentialSerializer(serializers.Serializer):
    id = serializers.CharField()
    type = serializers.CharField()


class AccountsAssertOptionsSerializer(serializers.Serializer):
    """The request options, verbatim from ``webauthn.authentication_options``.

    The ceremony sends an empty body: this view confirms no factor, because
    the tap it asks for is the factor.
    """

    challenge = serializers.CharField()
    timeout = serializers.IntegerField(required=False)
    rpId = serializers.CharField()
    allowCredentials = AccountsAssertionCredentialSerializer(many=True)
    userVerification = serializers.CharField(required=False)


class AccountsAssertRequestSerializer(serializers.Serializer):
    #: The browser's ``navigator.credentials.get()`` result; the shape is the
    #: WebAuthn spec's, and verification happens inside the library.
    credential = serializers.DictField()


class AccountsKeyTokenSerializer(serializers.Serializer):
    """The one-time token a key tap buys — what the next confirmation sends
    as ``totp``."""

    token = serializers.CharField()


class AccountsRenameKeyRequestSerializer(serializers.Serializer):
    name = serializers.CharField()


class AccountsRemoveKeyRequestSerializer(serializers.Serializer):
    totp = serializers.CharField(required=False, allow_blank=True)


class AccountsCapabilitiesSerializer(serializers.Serializer):
    """App name to its grantable verbs, "view" first. Every app in the
    vocabulary is one of these; an install without one omits its key."""

    hosts = serializers.ListField(child=serializers.CharField(), required=False)
    tasks = serializers.ListField(child=serializers.CharField(), required=False)
    playbooks = serializers.ListField(child=serializers.CharField(), required=False)
    automations = serializers.ListField(child=serializers.CharField(), required=False)
    alerts = serializers.ListField(child=serializers.CharField(), required=False)
    statuspages = serializers.ListField(child=serializers.CharField(), required=False)
    reprovision = serializers.ListField(child=serializers.CharField(), required=False)


class AccountsSessionRemainingSerializer(serializers.Serializer):
    """Seconds left before each limit ends the session (never negative)."""

    idle_seconds_left = serializers.IntegerField()
    max_seconds_left = serializers.IntegerField()
    idle_seconds = serializers.IntegerField()
    max_seconds = serializers.IntegerField()


class AccountsSettingsAccessSerializer(serializers.Serializer):
    """One key per settings page (``permissions.SETTINGS_ACCESS``), so the
    document lists the pages this build ships."""

    account = serializers.CharField()
    agents = serializers.CharField()
    api = serializers.CharField()
    users = serializers.CharField()
    sites = serializers.CharField()
    branding = serializers.CharField()
    display = serializers.CharField()
    retention = serializers.CharField()
    relays = serializers.CharField()
    distribution = serializers.CharField()
    images = serializers.CharField()
    license = serializers.CharField()
    email = serializers.CharField()
    ai = serializers.CharField()
    identity = serializers.CharField()
    directory = serializers.CharField()
    scanners = serializers.CharField()
    jackil = serializers.CharField()


class AccountsTotpStatusSerializer(serializers.Serializer):
    enrolled = serializers.BooleanField()
    pending = serializers.BooleanField()
    keys = serializers.IntegerField()
    keys_possible = serializers.BooleanField()
    keys_reason = serializers.CharField(allow_blank=True)


class AccountsTotpEnrollStartSerializer(serializers.Serializer):
    secret = serializers.CharField(help_text="Base32 secret; the QR encodes otpauth_uri.")
    otpauth_uri = serializers.CharField()


class AccountsTotpCodeSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="The current 6-digit code. DEBUG builds only.")


class AccountsTotpEnrolledSerializer(serializers.Serializer):
    """POST totp/enroll/ and totp/disable answer with the resulting state."""

    enrolled = serializers.BooleanField()


class AccountsTotpConfirmRequestSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="A current 6-digit code, or a wa_ key token.")


class AccountsUserRowSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)
    role = serializers.ChoiceField(choices=["owner", "admin", "operator", "viewer", "none"])
    by_site = serializers.BooleanField()
    is_active = serializers.BooleanField()
    date_joined = serializers.DateTimeField()


class AccountsUsersIndexSerializer(serializers.Serializer):
    seats_used = serializers.IntegerField()
    seats_allowed = serializers.IntegerField()
    users = AccountsUserRowSerializer(many=True)


class AccountsUserCreateSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    role = serializers.CharField(
        required=False, allow_blank=True,
        help_text="Optional on create; required by the role endpoint. PATCH "
                  "changes the role alone and ignores the other fields. An "
                  "unknown role is a 400.")


class AccountsDetailSerializer(serializers.Serializer):
    """What these views refuse with — DRF's ErrorDetail renders as ``detail``."""

    detail = serializers.CharField()


class AccountsUpgradeSerializer(serializers.Serializer):
    """What a feature the license does not cover answers with: 402 here.

    The body is vigil.licensing.upgrade_body's, which every app that gates on
    a feature returns verbatim; accounts spells its other refusals ``detail``
    (see ``AccountsDetailSerializer``).
    """

    feature = serializers.CharField()
    licensed = serializers.BooleanField()
    detail = serializers.CharField()
    upgrade_url = serializers.URLField()
