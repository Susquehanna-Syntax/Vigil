"""Compile a policy to an ordinary task definition.

A policy never acts on its own. Each save (and each run, so the patch lists
are current) writes a TaskDefinition called ``Policy: <name>`` through the
normal YAML → ``parse_and_validate`` path, and every dispatch signs it like
any other task. An admin can open that definition, read exactly what will
run, and copy its steps into a task of their own.
"""

from __future__ import annotations

import re

import yaml

from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate

from .models import UpdatePolicy

_PLAIN_INT = re.compile(r"^\d{1,2}$")


def definition_name(policy: UpdatePolicy) -> str:
    return f"Policy: {policy.name}"[:120]


def app_steps(policy: UpdatePolicy) -> list[dict]:
    steps = []
    for n, rule in enumerate(policy.app_rules.all(), start=1):
        params = {"app": rule.app, "state": rule.state}
        if rule.source:
            params["source"] = rule.source
        if rule.version:
            params["version"] = rule.version
        steps.append({"id": f"app-{n}", "type": "app_ensure", "params": params})
    return steps


def patch_steps(policy: UpdatePolicy) -> list[dict]:
    """The Patching tab's steps (phase 07). None until then."""
    return []


def window_schedule(policy: UpdatePolicy) -> dict | None:
    """The policy's window as a task ``schedule.window``, when the cron is
    simple enough to say as one (a single hour and minute, plain weekdays).

    Only a wave rollout carries it: its later waves are dispatched hours or
    days after the run started, and must still land inside a window. A
    direct run expires its tasks when the window closes instead.
    """
    if policy.window_hours >= 24:
        return None
    if not (_PLAIN_INT.match(policy.cron_minute) and _PLAIN_INT.match(policy.cron_hour)):
        return None
    hour, minute = int(policy.cron_hour), int(policy.cron_minute)
    if hour > 23 or minute > 59:
        return None
    window = {"start_hour": hour, "start_minute": minute,
              "end_hour": (hour + policy.window_hours - 1) % 24,
              "end_minute": minute}
    if policy.cron_dow != "*":
        parts = policy.cron_dow.split(",")
        if not all(_PLAIN_INT.match(p) and int(p) <= 7 for p in parts):
            return None
        # cron counts from Sunday (0 or 7); the task window from Monday.
        window["days"] = sorted({(int(p) + 6) % 7 for p in parts})
    return {"window": window}


def policy_yaml(policy: UpdatePolicy) -> str | None:
    """The definition's YAML, or None when the policy has nothing to do."""
    actions = app_steps(policy) + patch_steps(policy)
    if not actions:
        return None
    risk = "standard"
    if any(a["type"] == "reboot" for a in actions):
        risk = "high"
    doc = {
        "name": definition_name(policy),
        "description": (f"Generated from the policy {policy.name!r}. Edit the "
                        f"policy, not this task — it is rewritten on every save."),
        "risk": risk,
    }
    if policy.wave_group_tag:
        schedule = window_schedule(policy)
        if schedule:
            doc["schedule"] = schedule
    doc["actions"] = actions
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def compile_policy(policy: UpdatePolicy) -> TaskDefinition | None:
    """Write (or refresh) the policy's task definition and return it.

    Raises SpecError if the YAML does not validate — validation.py refuses
    every policy that could produce one, so that is a bug, not user error.
    """
    source = policy_yaml(policy)
    if source is None:
        return policy.task_definition
    spec = parse_and_validate(source)
    definition = policy.task_definition or TaskDefinition(
        owner=policy.created_by, visibility=TaskDefinition.Visibility.PRIVATE)
    definition.yaml_source = source
    definition.parsed_spec = spec
    definition.name = spec["name"]
    definition.description = spec["description"]
    definition.relevance = spec["relevance"]
    definition.risk_level = spec["risk"]
    definition.save()
    if policy.task_definition_id != definition.pk:
        policy.task_definition = definition
        policy.save(update_fields=["task_definition"])
    return definition
