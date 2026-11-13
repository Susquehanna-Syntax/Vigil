"""OpenAPI shapes of the playbooks endpoints (sprint M5).

Documentation only: the views build their responses by hand, and these
serializers describe what they return so the schema has real types. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class PlaybookInputSerializer(serializers.Serializer):
    """One declared playbook input, as ``_validate_inputs`` canonicalises it.

    A choice input's ``default`` is one of the option values (text), and a
    number input's is a number, so ``default`` is free-form here.
    """

    id = serializers.CharField()
    type = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    choices = serializers.ListField(
        child=serializers.DictField(child=serializers.CharField()))
    default = serializers.JSONField(allow_null=True)
    required = serializers.BooleanField()


class PlaybookStepSerializer(serializers.Serializer):
    definition_id = serializers.CharField()
    definition_name = serializers.CharField()
    risk = serializers.CharField()
    order = serializers.IntegerField()
    params_override = serializers.DictField()
    inputs = serializers.DictField(child=serializers.CharField())
    on_not_applicable = serializers.CharField()
    on_failure = serializers.CharField()
    color = serializers.CharField(allow_blank=True)
    outcome = serializers.CharField(allow_blank=True)
    step_id = serializers.CharField()
    branch = serializers.CharField(allow_blank=True)


class PlaybookFlowSerializer(serializers.Serializer):
    """One node of ``playbook.flow``: a step reference, a go-to, or a branch.

    A node carries the keys of exactly one of those three; every field is
    therefore optional. Branch lanes nest, and DRF serializers cannot be
    recursive by class (the body cannot name itself), so lane *items* are
    documented as free-form dicts while the lanes themselves are lists.
    """

    id = serializers.CharField(required=False)
    goto = serializers.CharField(required=False)
    max = serializers.IntegerField(required=False)
    step = serializers.CharField(required=False)
    if_ = serializers.CharField(source="if", required=False)
    then = serializers.ListField(child=serializers.DictField(), required=False)
    elif_ = serializers.ListField(child=serializers.DictField(),
                                  source="elif", required=False)
    else_ = serializers.ListField(child=serializers.DictField(),
                                  source="else", required=False)


class PlaybookSerializer(serializers.Serializer):
    """``_row``: the playbook with its steps. ``flow`` is a list of nodes or
    null for a plain ordered playbook."""

    id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    target_tags = serializers.ListField(child=serializers.CharField())
    auto_enroll = serializers.BooleanField()
    archived_at = serializers.DateTimeField(allow_null=True)
    completion_tag = serializers.CharField(allow_blank=True)
    allow_high_risk = serializers.BooleanField()
    inputs = PlaybookInputSerializer(many=True)
    site_id = serializers.CharField(allow_null=True)
    tasks_auto_sync = serializers.BooleanField()
    stale_steps = serializers.ListField(child=serializers.CharField())
    created_at = serializers.DateTimeField()
    failing_hosts = serializers.IntegerField()
    steps = PlaybookStepSerializer(many=True)
    flow = serializers.ListField(child=PlaybookFlowSerializer(), allow_null=True)


class PlaybookCreateSerializer(serializers.Serializer):
    """The create body. A step entry is a bare task id or a dict of the
    authoring fields, so the step lists are free-form here; ``inputs`` is
    raw input, which the view validates — :class:`PlaybookInputSerializer`
    is its canonical shape once stored."""

    name = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True)
    target_tags = serializers.ListField(child=serializers.CharField(),
                                        required=False)
    completion_tag = serializers.CharField(required=False, allow_blank=True)
    site_id = serializers.CharField(required=False, allow_null=True)
    allow_high_risk = serializers.BooleanField(required=False)
    inputs = serializers.ListField(child=serializers.DictField(),
                                   required=False)
    definition_ids = serializers.ListField(child=serializers.DictField(),
                                           required=False)
    flow_steps = serializers.ListField(child=serializers.DictField(),
                                       required=False)


class PlaybookPatchSerializer(serializers.Serializer):
    """Every field of :class:`PlaybookCreateSerializer` that a PATCH reads,
    each optional."""

    name = serializers.CharField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    target_tags = serializers.ListField(child=serializers.CharField(),
                                        required=False)
    completion_tag = serializers.CharField(required=False, allow_blank=True)
    auto_enroll = serializers.BooleanField(required=False)
    tasks_auto_sync = serializers.BooleanField(required=False)
    site_id = serializers.CharField(required=False, allow_null=True)
    allow_high_risk = serializers.BooleanField(required=False)
    inputs = serializers.ListField(child=serializers.DictField(),
                                   required=False)
    definition_ids = serializers.ListField(child=serializers.DictField(),
                                           required=False)
    flow_steps = serializers.ListField(child=serializers.DictField(),
                                       required=False)


class PlaybookDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()
    needs_totp = serializers.BooleanField(required=False)


class PlaybookYamlSerializer(serializers.Serializer):
    yaml = serializers.CharField()
    filename = serializers.CharField()


class PlaybookFromYamlSerializer(serializers.Serializer):
    yaml = serializers.CharField(help_text="The community playbook document.")
    playbook_id = serializers.CharField(
        required=False, help_text="Replace this playbook in place instead of creating one.")
    site_id = serializers.CharField(required=False, allow_null=True)


class PlaybookLibraryTemplateSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    steps = serializers.ListField(child=serializers.CharField())
    allow_high_risk = serializers.BooleanField()
    inputs = serializers.ListField(child=serializers.DictField())


class PlaybookLibrarySerializer(serializers.Serializer):
    results = PlaybookLibraryTemplateSerializer(many=True)


class PlaybookArchiveSerializer(serializers.Serializer):
    restore = serializers.BooleanField(required=False)


class PlaybookFailureSerializer(serializers.Serializer):
    host_id = serializers.CharField()
    hostname = serializers.CharField()
    status = serializers.CharField()
    state = serializers.CharField(required=False)
    step_label = serializers.CharField(required=False)
    output = serializers.CharField(required=False)
    at = serializers.DateTimeField(required=False, allow_null=True)


class PlaybookRetrySerializer(serializers.Serializer):
    host = serializers.CharField(required=False,
                                 help_text="Retry one held-back host; omit for all of them.")


class PlaybookDispatchSerializer(serializers.Serializer):
    dispatched = serializers.IntegerField()
    hosts = serializers.ListField(child=serializers.CharField())
