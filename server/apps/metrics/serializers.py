from rest_framework import serializers

from .models import MetricPoint


class MetricPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = MetricPoint
        fields = ["time", "value", "labels"]