"""OpenAPI shapes of the tasks rollout and wave endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class TasksRolloutWaveSerializer(serializers.Serializer):
    """One lane of ``_rollout_wave_progress``: the wave plus its task counts."""

    id = serializers.CharField()
    name = serializers.CharField()
    order = serializers.IntegerField()
    tags = serializers.ListField(child=serializers.CharField())
    validation_hours = serializers.IntegerField()
    enabled = serializers.BooleanField()
    hosts = serializers.IntegerField()
    tasks_total = serializers.IntegerField()
    tasks_done = serializers.IntegerField()
    tasks_failed = serializers.IntegerField()
    status = serializers.CharField()


class TasksRolloutSerializer(serializers.Serializer):
    """``_RolloutDetailSerializer``: the rollout header with its ``waves``."""

    id = serializers.CharField()
    action_kind = serializers.CharField()
    definition = serializers.CharField(allow_null=True)
    definition_name = serializers.CharField(allow_null=True)
    playbook = serializers.CharField(allow_null=True)
    playbook_name = serializers.CharField(allow_null=True)
    target_name = serializers.CharField()
    state = serializers.CharField()
    current_wave = serializers.IntegerField(allow_null=True)
    current_wave_name = serializers.CharField(allow_null=True)
    current_wave_order = serializers.IntegerField(allow_null=True)
    wave_group_tag = serializers.CharField(allow_blank=True)
    batch = serializers.CharField(allow_null=True)
    failure_threshold_pct = serializers.IntegerField()
    min_results_before_halt = serializers.IntegerField()
    halted_reason = serializers.CharField(allow_blank=True)
    halted_by_name = serializers.CharField(allow_null=True)
    resumed_by_name = serializers.CharField(allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    wave_started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    created_by_name = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()
    waves = TasksRolloutWaveSerializer(many=True)


class TasksRolloutBatchSerializer(serializers.Serializer):
    """The body of a parallel-group start: one rollout per wave group."""

    batch = serializers.CharField()
    rollouts = TasksRolloutSerializer(many=True)


class TasksRolloutStartSerializer(serializers.Serializer):
    """One ``POST /rollouts/``. ``wave_group_tags`` starts a batch — then the
    response is ``TasksRolloutBatchSerializer`` instead of one rollout."""

    definition_id = serializers.CharField(
        required=False, help_text="Task definition to roll out. Supply exactly "
        "one of definition_id or playbook_id.")
    playbook_id = serializers.CharField(required=False)
    wave_group_tag = serializers.CharField(
        required=False, allow_blank=True,
        help_text="One wave ladder by group tag; empty walks every enabled wave.")
    wave_group_tags = serializers.ListField(
        child=serializers.CharField(), required=False,
        help_text="Two or more ladders to start as one batch. Mutually exclusive "
        "with wave_group_tag.")
    failure_threshold_pct = serializers.IntegerField(required=False)
    min_results_before_halt = serializers.IntegerField(required=False)
    inputs = serializers.DictField(
        required=False, help_text="Input id to value, for a target that declares inputs.")
    totp = serializers.CharField(help_text="The six-digit confirmation code.")


class TasksRolloutStartResponseSerializer(serializers.Serializer):
    """``POST /rollouts/``. A single-ladder start returns the rollout fields
    beside ``batch`` (null); a ``wave_group_tags`` batch returns ``batch`` and
    one rollout per ladder instead."""

    id = serializers.CharField(required=False)
    action_kind = serializers.CharField(required=False)
    definition = serializers.CharField(allow_null=True, required=False)
    definition_name = serializers.CharField(allow_null=True, required=False)
    playbook = serializers.CharField(allow_null=True, required=False)
    playbook_name = serializers.CharField(allow_null=True, required=False)
    target_name = serializers.CharField(required=False)
    state = serializers.CharField(required=False)
    current_wave = serializers.IntegerField(allow_null=True, required=False)
    current_wave_name = serializers.CharField(allow_null=True, required=False)
    current_wave_order = serializers.IntegerField(allow_null=True, required=False)
    wave_group_tag = serializers.CharField(allow_blank=True, required=False)
    batch = serializers.CharField(allow_null=True)
    failure_threshold_pct = serializers.IntegerField(required=False)
    min_results_before_halt = serializers.IntegerField(required=False)
    halted_reason = serializers.CharField(allow_blank=True, required=False)
    halted_by_name = serializers.CharField(allow_null=True, required=False)
    resumed_by_name = serializers.CharField(allow_null=True, required=False)
    started_at = serializers.DateTimeField(allow_null=True, required=False)
    wave_started_at = serializers.DateTimeField(allow_null=True, required=False)
    finished_at = serializers.DateTimeField(allow_null=True, required=False)
    created_by_name = serializers.CharField(allow_null=True, required=False)
    created_at = serializers.DateTimeField(required=False)
    waves = TasksRolloutWaveSerializer(many=True, required=False)
    rollouts = TasksRolloutSerializer(many=True, required=False)


class TasksRolloutHaltSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)
    totp = serializers.CharField()


class TasksBatchHaltSerializer(serializers.Serializer):
    halted = serializers.IntegerField(help_text="How many rollouts the halt stopped.")


class TasksWaveHostSerializer(serializers.Serializer):
    task_id = serializers.CharField()
    host_id = serializers.CharField()
    hostname = serializers.CharField()
    ip_address = serializers.CharField(allow_null=True)
    state = serializers.CharField()
    failed = serializers.BooleanField()
    dispatched_at = serializers.DateTimeField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)
    step_label = serializers.CharField(allow_blank=True)
    output = serializers.CharField(allow_blank=True)


class TasksWaveHostsSerializer(serializers.Serializer):
    rollout_id = serializers.CharField()
    wave_id = serializers.IntegerField()
    total = serializers.IntegerField()
    failed = serializers.IntegerField()
    hosts = TasksWaveHostSerializer(many=True)


class TasksWaveGroupSerializer(serializers.Serializer):
    tag = serializers.CharField()
    waves = serializers.IntegerField()
    enabled_waves = serializers.IntegerField()


class TasksWaveGroupsSerializer(serializers.Serializer):
    groups = TasksWaveGroupSerializer(many=True)
    ungrouped_waves = serializers.IntegerField()


class TasksWaveSerializer(serializers.Serializer):
    """``PatchWaveSerializer``: a wave, its host counts, and its site."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    order = serializers.IntegerField()
    tags = serializers.ListField(child=serializers.CharField())
    group_tags = serializers.ListField(child=serializers.CharField())
    validation_hours = serializers.IntegerField()
    enabled = serializers.BooleanField()
    host_count = serializers.IntegerField()
    exclusive_host_count = serializers.IntegerField()
    site_id = serializers.CharField(allow_null=True)


class TasksWaveWriteSerializer(serializers.Serializer):
    """What ``wave_collection``/``wave_detail`` read from the request body.

    ``site_id`` is write-only: the response carries it, so it cannot also be a
    response field of ``TasksWaveSerializer`` — the component would carry the
    key twice and the schema would be wrong for one of the two.
    """

    name = serializers.CharField(required=False)
    order = serializers.IntegerField(required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    group_tags = serializers.ListField(child=serializers.CharField(), required=False)
    validation_hours = serializers.IntegerField(required=False)
    enabled = serializers.BooleanField(required=False)
    site_id = serializers.CharField(
        required=False, allow_null=True,
        help_text="The site whose ladder to put the wave on; empty for global.")


class TasksWaveOrderConflictSerializer(serializers.Serializer):
    """DRF field errors and the order-collision refusal: per-field messages."""

    detail = serializers.CharField(required=False)
    name = serializers.ListField(child=serializers.CharField(), required=False)
    order = serializers.ListField(child=serializers.CharField(), required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    group_tags = serializers.ListField(child=serializers.CharField(), required=False)
    validation_hours = serializers.ListField(child=serializers.CharField(),
                                             required=False)


class TasksDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()
