"""OpenAPI shapes of the automation endpoints (sprint M5).

Documentation only: ``views._row`` builds the automation dict by hand and the
JSON body has no serializer, so these describe what the views return. Nothing
validates or renders through them.
"""
from rest_framework import serializers


class AutomationDetailErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()


class AutomationTotpErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
    needs_totp = serializers.BooleanField()


class AutomationCronSerializer(serializers.Serializer):
    minute = serializers.CharField()
    hour = serializers.CharField()
    dom = serializers.CharField()
    month = serializers.CharField()
    dow = serializers.CharField()


class AutomationRowSerializer(serializers.Serializer):
    """One automation, as ``_row`` renders it."""

    id = serializers.CharField()
    name = serializers.CharField()
    enabled = serializers.BooleanField()
    site_id = serializers.CharField(allow_null=True)
    allow_high_risk = serializers.BooleanField()
    trigger = serializers.CharField()
    event = serializers.CharField()
    min_severity = serializers.CharField()
    event_tags = serializers.ListField(child=serializers.CharField())
    event_rule = serializers.CharField(allow_null=True)
    event_rule_name = serializers.CharField(allow_null=True)
    event_host = serializers.CharField(allow_null=True)
    event_host_name = serializers.CharField(allow_null=True)
    match_text = serializers.CharField()
    match_field = serializers.CharField()
    match_mode = serializers.CharField()
    condition_logic = serializers.CharField()
    conditions = serializers.ListField(child=serializers.DictField())
    cron = AutomationCronSerializer()
    cron_display = serializers.CharField()
    action_kind = serializers.CharField()
    dispatch_mode = serializers.CharField()
    task_definition = serializers.CharField(allow_null=True)
    task_auto_sync = serializers.BooleanField()
    task_stale = serializers.BooleanField()
    task_name = serializers.CharField(allow_null=True)
    playbook_name = serializers.CharField()
    params_override = serializers.DictField()
    inputs = serializers.DictField()
    target = serializers.CharField()
    target_tags = serializers.ListField(child=serializers.CharField())
    target_host = serializers.CharField(allow_null=True)
    last_run = serializers.CharField(allow_null=True)
    run_count = serializers.IntegerField()


class AutomationConditionFieldSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()
    kind = serializers.CharField()
    help = serializers.CharField()
    operators = serializers.ListField(child=serializers.CharField())


class AutomationOperatorSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()


class AutomationConditionMetaSerializer(serializers.Serializer):
    """``apps.automations.conditions.meta()``: the editor's field and operator
    surface."""

    fields = AutomationConditionFieldSerializer(many=True)
    operators = AutomationOperatorSerializer(many=True)
    valueless = serializers.ListField(child=serializers.CharField())


class AutomationIndexSerializer(serializers.Serializer):
    automations = AutomationRowSerializer(many=True)
    events = serializers.DictField(child=serializers.CharField())
    condition_meta = AutomationConditionMetaSerializer()


class AutomationWriteRequestSerializer(serializers.Serializer):
    """Every body field ``_apply`` reads. All optional: the view sets only the
    keys the request carries, and a PATCH may carry one."""

    name = serializers.CharField(required=False)
    enabled = serializers.BooleanField(required=False)
    site_id = serializers.CharField(required=False, allow_null=True)
    event = serializers.CharField(required=False, allow_blank=True)
    min_severity = serializers.CharField(required=False, allow_blank=True)
    event_rule = serializers.CharField(required=False, allow_null=True)
    event_tags = serializers.ListField(child=serializers.CharField(), required=False)
    event_host = serializers.CharField(required=False, allow_null=True)
    match_text = serializers.CharField(required=False, allow_blank=True)
    conditions = serializers.ListField(child=serializers.DictField(), required=False)
    cron = serializers.DictField(required=False)
    dispatch_mode = serializers.CharField(required=False)
    task_definition = serializers.CharField(required=False, allow_null=True)
    task_auto_sync = serializers.BooleanField(required=False)
    playbook_name = serializers.CharField(required=False, allow_blank=True)
    params_override = serializers.DictField(required=False)
    inputs = serializers.DictField(required=False)
    target_tags = serializers.ListField(child=serializers.CharField(), required=False)
    target_host = serializers.CharField(required=False, allow_null=True)
    # The guarded and enumerated ones, typed loosely on purpose: `trigger`,
    # `action_kind`, `match_field`, `match_mode`, `condition_logic` and
    # `target` each take one of the model's choices, and `allow_high_risk`
    # turns the 2FA gate on or off.
    trigger = serializers.CharField(required=False)
    action_kind = serializers.CharField(required=False)
    match_field = serializers.CharField(required=False)
    match_mode = serializers.CharField(required=False)
    condition_logic = serializers.CharField(required=False)
    target = serializers.CharField(required=False)
    allow_high_risk = serializers.BooleanField(required=False)


class AutomationRunNowSerializer(serializers.Serializer):
    dispatched = serializers.IntegerField()
    reason = serializers.CharField(help_text="Why nothing was dispatched; empty when something was.")


class AutomationYamlSerializer(serializers.Serializer):
    yaml = serializers.CharField()
    filename = serializers.CharField()


class AutomationFromYamlRequestSerializer(serializers.Serializer):
    yaml = serializers.CharField(help_text="The community YAML document.")
    automation_id = serializers.CharField(
        required=False, help_text="Replace this automation instead of creating one.")
    site_id = serializers.CharField(required=False, allow_null=True)
