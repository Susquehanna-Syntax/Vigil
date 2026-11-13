"""OpenAPI shapes of the alert endpoints (sprint M5).

Documentation only: the views build their responses by hand or through the
rendering ``AlertSerializer``, and these serializers describe what they return
so the schema has real types. Nothing validates or renders through them.
"""
from rest_framework import serializers


class AlertErrorSerializer(serializers.Serializer):
    error = serializers.CharField()


class AlertRuleRowSerializer(serializers.Serializer):
    """One row of the rule picker (``/alerts/rules/``)."""

    id = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField()
    severity = serializers.CharField()
    metric = serializers.CharField()


class AlertRuleBriefSerializer(serializers.Serializer):
    """The rule an alert fired for, nested in the alert. Null when Vigil
    raised the alert itself rather than a metric rule."""

    id = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField()
    metric = serializers.CharField()
    operator = serializers.CharField()
    threshold = serializers.FloatField()
    severity = serializers.CharField()


class AlertResponseSerializer(serializers.Serializer):
    """An alert as the API returns it — the rendering serializer's fields."""

    id = serializers.CharField()
    host = serializers.CharField()
    host_hostname = serializers.CharField()
    rule = AlertRuleBriefSerializer(allow_null=True)
    state = serializers.CharField()
    severity = serializers.CharField()
    message = serializers.CharField()
    metric_value = serializers.FloatField(allow_null=True)
    fired_at = serializers.DateTimeField()
    acknowledged_at = serializers.DateTimeField(allow_null=True)
    acknowledged_until = serializers.DateTimeField(allow_null=True)
    resolved_at = serializers.DateTimeField(allow_null=True)
    fix_context = serializers.DictField()


class AlertBulkRequestSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.CharField())
    action = serializers.ChoiceField(choices=["acknowledge", "unacknowledge"])
    duration_seconds = serializers.IntegerField(
        required=False, help_text="Acknowledgement length; absent means forever.")


class AlertDurationRequestSerializer(serializers.Serializer):
    duration_seconds = serializers.IntegerField(
        required=False, help_text="Acknowledgement length; absent means forever.")


class AlertBulkResultSerializer(serializers.Serializer):
    updated = serializers.IntegerField()
    skipped = serializers.IntegerField()
