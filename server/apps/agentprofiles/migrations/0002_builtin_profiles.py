"""The three built-in profiles the wizard starts from.

Editable afterwards — an admin who wants "Managed server" to also pull images
should edit it rather than fork it — but never deletable, so there is always
something to copy from. Reversing is a no-op for the same reason deleting is
refused: a rolled-back deploy should not quietly take the starting points away.
"""

from django.db import migrations

#: The spec's list, filtered against AGENT_ACTIONS by hand below at runtime so
#: an action the agent no longer knows never lands in a seeded allowlist.
MANAGED_ALLOWLIST = (
    "check_service", "restart_service", "start_service", "stop_service",
    "reload_service", "check_docker_updates", "pull_image",
    "recreate_container", "restart_container", "run_package_updates",
    "update_agent", "clear_temp_files", "app_inventory", "windows_update_scan",
)

PROFILES = [
    {
        "name": "Monitor only",
        "description": "Reports inventory and metrics. Runs no tasks at all.",
        "mode": "monitor",
    },
    {
        "name": "Managed server",
        "description": "Runs the tasks in its allowlist: services, containers, "
                       "packages, updates and housekeeping.",
        "mode": "managed",
        "allowlist": list(MANAGED_ALLOWLIST),
    },
    {
        "name": "Full control workstation",
        "description": "Runs anything, including one-off commands, remote shell "
                       "and remote desktop.",
        "mode": "full_control",
        "allow_remote_shell": True,
        "allow_remote_desktop": True,
    },
]


def seed(apps, schema_editor):
    AgentProfile = apps.get_model("agentprofiles", "AgentProfile")
    from apps.tasks.refusals import AGENT_ACTIONS

    for spec in PROFILES:
        fields = {k: v for k, v in spec.items() if k != "name"}
        if "allowlist" in fields:
            fields["allowlist"] = sorted(
                a for a in fields["allowlist"] if a in AGENT_ACTIONS)
        AgentProfile.objects.update_or_create(
            name=spec["name"], defaults={**fields, "builtin": True})


class Migration(migrations.Migration):
    dependencies = (("agentprofiles", "0001_initial"),)

    operations = (migrations.RunPython(seed, migrations.RunPython.noop),)
