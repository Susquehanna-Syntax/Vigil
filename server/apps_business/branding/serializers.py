import re

from rest_framework import serializers

from .models import BrandingConfig

HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class BrandingConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = BrandingConfig
        fields = ["product_name", "logo_url", "accent", "footer_text", "support_url", "updated_at"]
        read_only_fields = ["updated_at"]

    def validate_accent(self, value):
        if value and not HEX_COLOR_RE.match(value):
            raise serializers.ValidationError(
                "Must be a hex color in the format #RRGGBB."
            )
        return value
