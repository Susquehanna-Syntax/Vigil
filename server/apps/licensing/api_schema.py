"""OpenAPI shapes of the licensing endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class LicenseSeatsSerializer(serializers.Serializer):
    used = serializers.IntegerField()
    allowed = serializers.IntegerField()


class LicenseFeatureSerializer(serializers.Serializer):
    name = serializers.CharField()
    tier = serializers.CharField()
    active = serializers.BooleanField()


class LicenseRefreshStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    at = serializers.DateTimeField(allow_null=True)
    result = serializers.CharField(allow_blank=True)
    detail = serializers.CharField(allow_blank=True)


class LicenseBannerSerializer(serializers.Serializer):
    severity = serializers.ChoiceField(choices=["info", "warning", "critical"])
    message = serializers.CharField()


class LicenseSerializer(serializers.Serializer):
    tier = serializers.CharField()
    status = serializers.CharField()
    detail = serializers.CharField(allow_blank=True)
    source = serializers.CharField(allow_blank=True)
    instance = serializers.CharField()
    org = serializers.CharField(allow_null=True)
    license_id = serializers.CharField(allow_null=True)
    expires = serializers.IntegerField(allow_null=True, help_text="Unix time")
    seats = LicenseSeatsSerializer()
    features = LicenseFeatureSerializer(many=True)
    banners = LicenseBannerSerializer(many=True)
    refresh = LicenseRefreshStatusSerializer()
    subscribe_url = serializers.URLField()


class LicensePasteSerializer(serializers.Serializer):
    license = serializers.CharField(help_text="The license token to install.")


class LicenseRefreshOutcomeSerializer(serializers.Serializer):
    result = serializers.CharField()
    detail = serializers.CharField(allow_blank=True)


class LicenseRefreshSerializer(LicenseSerializer):
    refresh_now = LicenseRefreshOutcomeSerializer(help_text="The outcome of this refresh.")


class DetailSerializer(serializers.Serializer):
    detail = serializers.CharField()
    status = serializers.CharField(required=False)
