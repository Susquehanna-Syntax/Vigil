"""OpenAPI shapes of the metrics endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class MetricSampleSerializer(serializers.Serializer):
    """One measurement of one series — what the history endpoint returns."""

    time = serializers.DateTimeField()
    value = serializers.FloatField()
    labels = serializers.DictField()


class MetricCatalogEntrySerializer(serializers.Serializer):
    category = serializers.CharField()
    metric = serializers.CharField()
    label = serializers.CharField()


def recent_host_entry() -> serializers.DictField:
    """One host's entry of the recent body — a ``category/metric`` key per
    requested series, each holding that host's newest points (below), newest
    first. Those keys are data rather than fields, so this is one free-form
    mapping to a point list. A function rather than a declared field because a
    declared one is deep-copied on every instantiation of its parent, and the
    copy loses the inner serializer's name before the schema sees it.
    """
    return serializers.DictField(child=serializers.ListField(
        child=MetricSampleSerializer()))


class MetricsRecentSerializer(serializers.Serializer):
    """What `GET /api/v1/metrics/recent/` returns: host id → `category/metric`
    → that series' newest points (``MetricSampleSerializer``), newest first.

    Both levels of keys are data — a uuid, and whatever the request asked for —
    so neither can be a declared field list, and drf-spectacular does not follow
    a DictField whose child is a serializer at all. This property hands out an
    *instance* field instead, which it renders as one free-form mapping to what
    sits under each host id, named after the thing that lives there.
    """

    @property
    def _declared_fields(self):
        if "hosts" in self.fields:
            return self.fields
        return {"hosts": serializers.DictField(child=recent_host_entry())}


class MetricErrorSerializer(serializers.Serializer):
    """What the refusals here say. This app spells the field ``error``."""

    error = serializers.CharField()
