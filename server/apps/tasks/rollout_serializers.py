from rest_framework import serializers

from .models import PatchWaveGroup, PatchRollout, PatchWave


class PatchRolloutSerializer(serializers.ModelSerializer):
    """Rollout header. The list/detail views attach a ``waves`` annotation
    (see ``views._rollout_wave_progress``) with per-wave counts."""

    definition_name = serializers.CharField(
        source="definition.name", read_only=True, default=None)
    playbook_name = serializers.CharField(
        source="playbook.name", read_only=True, default=None)
    target_name = serializers.CharField(read_only=True)
    current_wave_name = serializers.CharField(
        source="current_wave.name", read_only=True, default=None)
    current_wave_order = serializers.IntegerField(
        source="current_wave.order", read_only=True, default=None)
    created_by_name = serializers.CharField(
        source="created_by.username", read_only=True, default=None)
    halted_by_name = serializers.CharField(
        source="halted_by.username", read_only=True, default=None)
    resumed_by_name = serializers.CharField(
        source="resumed_by.username", read_only=True, default=None)

    wave_group_name = serializers.CharField(
        source="wave_group.name", read_only=True, default="")

    class Meta:
        model = PatchRollout
        fields = [
            "id", "action_kind", "definition", "definition_name",
            "playbook", "playbook_name", "target_name", "state",
            "current_wave", "current_wave_name", "current_wave_order",
            "wave_group", "wave_group_name",
            "failure_threshold_pct", "min_results_before_halt",
            "halted_reason", "halted_by_name", "resumed_by_name",
            "started_at", "wave_started_at", "finished_at",
            "created_by_name", "created_at",
        ]
        read_only_fields = fields


class PatchWaveSerializer(serializers.ModelSerializer):
    """A wave: an ordered, tag-matched group of hosts.

    ``host_count`` and ``exclusive_host_count`` are both reported because they
    differ in a way that matters. A host in two waves is patched in the earlier
    one only, so a later wave's *matching* count overstates what it will
    actually touch. The editor shows both so an operator can see the overlap
    rather than being surprised by it mid-rollout.
    """

    host_count = serializers.SerializerMethodField()
    exclusive_host_count = serializers.SerializerMethodField()

    class Meta:
        model = PatchWave
        fields = [
            "id", "name", "order", "tags", "validation_hours", "enabled",
            "group", "host_count", "exclusive_host_count",
        ]

    def get_host_count(self, obj) -> int:
        from .models import wave_host_ids
        return len(wave_host_ids(obj))

    def get_exclusive_host_count(self, obj) -> int:
        """Hosts this wave actually patches, after earlier waves claim theirs."""
        from .models import PatchWave, rollout_wave_plan
        # Scoped to the wave's own group: a host claimed by an earlier wave of
        # a different ladder is not claimed from this one.
        siblings = PatchWave.objects.filter(
            enabled=True, group_id=obj.group_id).order_by("order")
        plan = rollout_wave_plan(list(siblings))
        return len(plan.get(obj.id, []))

    def validate_tags(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("tags must be a list")
        cleaned = [str(t).strip() for t in value if str(t).strip()]
        if len(cleaned) > 32:
            raise serializers.ValidationError("a wave may match at most 32 tags")
        # A wave with no tags matches nothing, which would silently skip it.
        if not cleaned:
            raise serializers.ValidationError(
                "a wave needs at least one tag — a wave with no tags patches nothing")
        return cleaned

    def validate_validation_hours(self, value):
        if value > 720:
            raise serializers.ValidationError(
                "validation window cannot exceed 720 hours (30 days)")
        return value


class PatchWaveGroupSerializer(serializers.ModelSerializer):
    wave_count = serializers.SerializerMethodField()

    class Meta:
        model = PatchWaveGroup
        fields = ["id", "name", "description", "is_default", "wave_count",
                  "created_at"]
        read_only_fields = ["id", "is_default", "created_at"]

    def get_wave_count(self, obj) -> int:
        return obj.waves.count()
