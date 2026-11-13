"""OpenAPI shapes of the AI suggest endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class AiDetailSerializer(serializers.Serializer):
    """A refusal. Named for its app: licensing already registers the plain
    ``detail`` body, and two components with one name collide in the schema."""

    detail = serializers.CharField()


# -- a configured provider -----------------------------------------------------

class AiProviderSerializer(serializers.Serializer):
    """One provider as the settings page lists it. The API key is write-only:
    ``api_key_set`` says whether one is stored, and never its value."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    kind = serializers.CharField()
    base_url = serializers.CharField(allow_blank=True)
    model = serializers.CharField(allow_blank=True)
    reasoning_effort = serializers.CharField(allow_blank=True)
    enabled = serializers.BooleanField()
    configured = serializers.BooleanField()
    order = serializers.IntegerField()


class AiProviderKeyStateSerializer(AiProviderSerializer):
    api_key_set = serializers.BooleanField()


class AiProviderCreateSerializer(serializers.Serializer):
    """A new provider: everything is optional, and the defaults say which
    kind of endpoint to call."""

    name = serializers.CharField(required=False)
    kind = serializers.CharField(required=False)
    base_url = serializers.CharField(required=False, allow_blank=True)
    model = serializers.CharField(required=False, allow_blank=True)
    reasoning_effort = serializers.CharField(required=False, allow_blank=True)
    enabled = serializers.BooleanField(required=False)
    order = serializers.IntegerField(required=False)
    api_key = serializers.CharField(required=False, allow_blank=True,
                                    help_text="Stored encrypted; never returned.")


class AiProviderPatchSerializer(serializers.Serializer):
    """Every field is optional: the settings page saves one widget at a time."""

    name = serializers.CharField(required=False)
    kind = serializers.CharField(required=False)
    base_url = serializers.CharField(required=False, allow_blank=True)
    model = serializers.CharField(required=False, allow_blank=True)
    reasoning_effort = serializers.CharField(required=False, allow_blank=True)
    enabled = serializers.BooleanField(required=False)
    order = serializers.IntegerField(required=False)
    api_key = serializers.CharField(required=False, allow_blank=True,
                                    help_text="Stored encrypted; never returned.")


# -- the connection test -------------------------------------------------------

class AiRejectedSuggestionSerializer(serializers.Serializer):
    yaml = serializers.CharField()
    error = serializers.CharField()


class ProviderTestOkSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    elapsed_ms = serializers.IntegerField()
    accepted = serializers.IntegerField()
    rejected = AiRejectedSuggestionSerializer(many=True)
    note = serializers.CharField(allow_blank=True)
    reply = serializers.CharField(allow_blank=True)


class ProviderTestFailureSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    error = serializers.CharField()
    elapsed_ms = serializers.IntegerField(required=False)


# -- a suggestion run ----------------------------------------------------------

class AiSuggestionSerializer(serializers.Serializer):
    yaml = serializers.CharField()
    # The validated task spec, in the shape apps.tasks.spec describes.
    parsed = serializers.DictField()
    risk = serializers.CharField()


class SuggestRunSerializer(serializers.Serializer):
    """One provider's answer: its tasks, the ones Vigil refused and why, and
    the prose the model wrote around them."""

    provider = AiProviderSerializer()
    suggestions = AiSuggestionSerializer(many=True)
    rejected = AiRejectedSuggestionSerializer(many=True)
    note = serializers.CharField(allow_blank=True)
    elapsed_ms = serializers.IntegerField()


class SuggestRunFailureSerializer(serializers.Serializer):
    """The provider answered nothing usable."""

    provider = AiProviderSerializer()
    error = serializers.CharField()
    elapsed_ms = serializers.IntegerField()


class AiSuggestRequestSerializer(serializers.Serializer):
    provider_id = serializers.IntegerField()
    note = serializers.CharField(required=False, allow_blank=True,
                                 help_text="Only read by the vulnerability run.")
    fix_key = serializers.CharField(required=False, allow_blank=True,
                                    help_text="Only read by the fix-group run.")
