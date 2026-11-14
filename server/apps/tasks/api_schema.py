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


# ── Definitions (sprint M5, phase 46) ───────────────────────────────────────
#
# The definition cards and the editor speak ``parsed_spec``: the canonical
# dict apps/tasks/spec/parse.py returns and stores on the row. Its nested
# trees — ``flow``, ``relevant``, a step's ``when:`` guard — are recursive, so
# they stay free-form ``DictField``s; everything with a fixed key set is a
# serializer below.


class TasksValidateRequestSerializer(serializers.Serializer):
    """What the definition create/update/validate views read from the body."""

    yaml_source = serializers.CharField(
        help_text="The task YAML. A save parses it and refuses a spec the "
        "server cannot deploy; validate only parses.")


class TasksDefinitionErrorSerializer(serializers.Serializer):
    """Every refusal these views makes is ``{"error": …}`` — a spec it cannot
    deploy, a definition owned by someone else, or a role that may not write."""

    error = serializers.CharField()


class TasksNotFoundSerializer(serializers.Serializer):
    """A 404 has one of two bodies: ``{"error"}`` when the row exists but the
    caller may not see it, DRF's ``{"detail"}`` when there is no such row."""

    error = serializers.CharField(required=False)
    detail = serializers.CharField(required=False)


class TasksParsedSpecActionSerializer(serializers.Serializer):
    """One flattened leaf step of ``parsed_spec.actions``. ``when`` is the
    guard as source text and ``branch`` the dot path of the branch it sits
    in, not a tree."""

    id = serializers.CharField()
    type = serializers.CharField()
    label = serializers.CharField(allow_blank=True)
    params = serializers.DictField()
    risk = serializers.CharField()
    when = serializers.CharField(allow_blank=True,
                                 help_text="The step's guard expression, as written.")
    timeout = serializers.IntegerField(allow_null=True,
                                       help_text="Per-step seconds; null for the default.")
    outcome = serializers.CharField(allow_null=True, allow_blank=True,
                                    help_text="The named end state a success here leaves the host in.")
    outputs = serializers.ListField(child=serializers.CharField())
    branch = serializers.CharField(allow_null=True, allow_blank=True,
                                   help_text="Dot path of the branch this step is in; null at top level.")
    # execute_script only: what the agent must have to run the body verbatim.
    script_sha256 = serializers.CharField(required=False)


class TasksParsedSpecSerializer(serializers.Serializer):
    """``parsed_spec`` as the editor and the library cards receive it."""

    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    relevance = serializers.CharField(allow_blank=True)
    uid = serializers.CharField(allow_blank=True, required=False,
                                help_text="This task's identity in the community catalog.")
    author = serializers.CharField(allow_blank=True, required=False)
    created = serializers.CharField(allow_blank=True, required=False,
                                    help_text="ISO-8601 date.")
    cves = serializers.ListField(child=serializers.CharField(), required=False)
    references = serializers.ListField(child=serializers.CharField(), required=False)
    platforms = serializers.ListField(child=serializers.CharField(), required=False)
    risk = serializers.CharField()
    declared_risk = serializers.CharField(required=False)
    actions = TasksParsedSpecActionSerializer(many=True)
    uses = serializers.ListField(child=serializers.CharField(), required=False,
                                 help_text="Task names pulled in with ``use:``; spliced in at deploy.")
    flow = serializers.DictField(allow_null=True, required=False,
                                 help_text="The if/then/else tree; null outside a branching task.")
    inputs = serializers.ListField(child=serializers.DictField(), required=False)
    relevant = serializers.DictField(allow_null=True, required=False)
    severity = serializers.CharField(required=False, allow_blank=True)
    boost = serializers.ListField(child=serializers.DictField(), required=False)
    schedule = serializers.DictField(required=False)
    on_failure = serializers.DictField(required=False)
    success_criteria = serializers.DictField(allow_null=True, required=False)
    collect = serializers.DictField(allow_null=True, required=False)
    target_tags = serializers.ListField(child=serializers.CharField(), required=False)
    warnings = serializers.ListField(child=serializers.CharField())


class TasksDefinitionSerializer(serializers.Serializer):
    """``TaskDefinitionSerializer``: one definition, as the library card and
    the editor read it.

    ``owner`` is a primary key and so an integer; ``id``, ``forked_from`` and
    the archive view's path parameter are UUIDs, which DRF renders as text.
    """

    id = serializers.CharField()
    archived_at = serializers.DateTimeField(allow_null=True)
    owner = serializers.IntegerField(allow_null=True)
    owner_username = serializers.CharField(allow_null=True)
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    relevance = serializers.CharField(allow_blank=True)
    risk_level = serializers.CharField()
    visibility = serializers.CharField()
    yaml_source = serializers.CharField(allow_blank=True)
    parsed_spec = TasksParsedSpecSerializer()
    action_count = serializers.IntegerField()
    forked_from = serializers.CharField(allow_null=True)
    content_source = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class TasksDefinitionListRowSerializer(serializers.Serializer):
    """A ``GET /definitions/`` row. Under the default scope every row also
    carries ``last_run``; under ``scope=community`` it never does."""

    id = serializers.CharField()
    archived_at = serializers.DateTimeField(allow_null=True)
    owner = serializers.IntegerField(allow_null=True)
    owner_username = serializers.CharField(allow_null=True)
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    relevance = serializers.CharField(allow_blank=True)
    risk_level = serializers.CharField()
    visibility = serializers.CharField()
    yaml_source = serializers.CharField(allow_blank=True)
    parsed_spec = TasksParsedSpecSerializer()
    action_count = serializers.IntegerField()
    forked_from = serializers.CharField(allow_null=True)
    content_source = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    last_run = serializers.DictField(
        allow_null=True, required=False,
        help_text="The newest run's header beside its summary — ``id``, "
        "``created_at``, ``state``, ``hosts``, ``states``, ``outcomes`` — "
        "without the per-host breakdown. Null when it has never run.")


class TasksValidateResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    parsed_spec = TasksParsedSpecSerializer()


class TasksEditorTemplateSerializer(serializers.Serializer):
    """One bundled template: its picker entry beside the YAML itself."""

    id = serializers.CharField()
    label = serializers.CharField()
    group = serializers.CharField(allow_blank=True)
    summary = serializers.CharField(allow_blank=True)
    yaml = serializers.CharField()


class TasksEditorTemplatesSerializer(serializers.Serializer):
    results = TasksEditorTemplateSerializer(many=True)


class TasksRefusalSerializer(serializers.Serializer):
    host_id = serializers.CharField()
    hostname = serializers.CharField()
    actions = serializers.ListField(child=serializers.CharField(),
                                    help_text="What that host's agent would refuse.")


class TasksRefusalsSerializer(serializers.Serializer):
    """Hosts that would refuse, from the agents' reported allowlists — a
    warning for the deploy dialog, never enforcement."""

    refusals = TasksRefusalSerializer(many=True)


class TasksTaskSerializer(serializers.Serializer):
    """``TaskSerializer``: one history row. ``nonce`` is deliberately absent —
    it is the agent's one-time replay token."""

    id = serializers.CharField()
    host = serializers.CharField()
    host_hostname = serializers.CharField()
    requested_by = serializers.CharField(allow_null=True)
    requested_by_username = serializers.CharField(allow_null=True)
    run = serializers.CharField(allow_null=True)
    step_order = serializers.IntegerField()
    step_label = serializers.CharField(allow_blank=True)
    action = serializers.CharField()
    params = serializers.DictField()
    risk_level = serializers.CharField()
    state = serializers.CharField()
    ttl_seconds = serializers.IntegerField()
    result_output = serializers.CharField(allow_blank=True)
    result_data = serializers.DictField()
    created_at = serializers.DateTimeField()
    dispatched_at = serializers.DateTimeField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)
    step_ref = serializers.CharField(allow_blank=True)
    branch = serializers.CharField(allow_blank=True)


class TasksArchiveSerializer(serializers.Serializer):
    """``POST .../archive/``: ``restore`` is the only body field, and its
    absence means archive rather than restore."""

    restore = serializers.BooleanField(
        required=False,
        help_text="Put the definition back in the working set instead of retiring it.")
