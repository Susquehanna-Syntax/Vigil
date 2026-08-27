from rest_framework import serializers

from .models import PatchRollout


class PatchRolloutSerializer(serializers.ModelSerializer):
    """Rollout header. The list/detail views attach a ``rings`` annotation
    (see ``views._rollout_ring_progress``) with per-ring counts."""

    definition_name = serializers.CharField(
        source="definition.name", read_only=True, default=None)
    current_ring_name = serializers.CharField(
        source="current_ring.name", read_only=True, default=None)
    current_ring_order = serializers.IntegerField(
        source="current_ring.order", read_only=True, default=None)
    created_by_name = serializers.CharField(
        source="created_by.username", read_only=True, default=None)
    halted_by_name = serializers.CharField(
        source="halted_by.username", read_only=True, default=None)
    resumed_by_name = serializers.CharField(
        source="resumed_by.username", read_only=True, default=None)

    class Meta:
        model = PatchRollout
        fields = [
            "id", "definition", "definition_name", "state",
            "current_ring", "current_ring_name", "current_ring_order",
            "failure_threshold_pct", "min_results_before_halt",
            "halted_reason", "halted_by_name", "resumed_by_name",
            "started_at", "ring_started_at", "finished_at",
            "created_by_name", "created_at",
        ]
        read_only_fields = fields
