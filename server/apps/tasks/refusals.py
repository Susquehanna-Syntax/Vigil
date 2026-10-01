"""Which hosts will refuse a task, before it is sent (M7 phase 08).

A managed agent runs only the actions in its local ``allowlist:``; a monitor
agent runs nothing; ``run_command`` needs full_control; the destructive
reprovision actions need ``allow_reprovision``. The agent reports its allowlist
at check-in, so the server can tell an operator which targets would refuse a
task instead of letting the task fail there. The agent stays the authority —
this is a warning, never enforcement.
"""
from __future__ import annotations

#: Every action the agent really runs — a copy of ``_ALL_ACTIONS`` in
#: ``agent/vigil_agent/config.py`` (the server never imports agent code), plus
#: the never-allowlistable ones. ``test_refusals`` checks the two stay equal.
#: Pseudo-actions the server expands or the agent handles itself (``playbook``,
#: ``_script`` …) are not here, so they are never reported as refused.
AGENT_ACTIONS = frozenset({
    "add_firewall_rule",
    "add_tag",
    "add_user_to_group",
    "app_ensure",
    "app_install",
    "app_install_custom",
    "app_inventory",
    "app_pin",
    "app_uninstall",
    "app_upgrade",
    "check_docker_updates",
    "check_service",
    "clear_docker_logs",
    "clear_temp_files",
    "container_logs",
    "container_rollback",
    "copy_file",
    "create_cron_job",
    "create_directory",
    "create_user",
    "delete_cron_job",
    "delete_path",
    "delete_user",
    "disable_firewall",
    "disable_service",
    "docker_compose_down",
    "docker_compose_up",
    "enable_firewall",
    "enable_service",
    "execute_script",
    "hunt_content",
    "hunt_file",
    "hunt_package",
    "hunt_port",
    "hunt_process",
    "hunt_registry",
    "hunt_service",
    "install_package",
    "list_firewall_rules",
    "move_file",
    "pull_image",
    "reboot",
    "recreate_container",
    "reload_service",
    "remove_container",
    "remove_firewall_rule",
    "remove_package",
    "remove_tag",
    "reprovision_preflight",
    "request_nessus_scan",
    "request_network_scan",
    "restart_container",
    "restart_service",
    "run_package_updates",
    "run_trivy_scan",
    "set_firewall_policy",
    "set_hostname",
    "set_permissions",
    "start_container",
    "stack_deploy",
    "stack_read",
    "stack_remove",
    "stack_restart",
    "stack_update",
    "start_service",
    "stop_container",
    "stop_service",
    "trivy_db_update",
    "update_agent",
    "update_container",
    "update_package",
    "windows_update_install",
    "windows_update_scan",
    "write_file",
    "run_command",
    "reprovision_stage",
    "reprovision_commit",
    "reprovision_cleanup",
})
_REPROVISION = frozenset({"reprovision_stage", "reprovision_commit", "reprovision_cleanup"})


def refused_actions(host, action_types: set[str]) -> list[str]:
    """Sorted actions in *action_types* this host's agent will refuse.

    ``[]`` when the agent has never reported an allowlist: unknown is not the
    same as refused, and an old agent must not look broken.
    """
    if host.agent_allowlist is None:
        return []
    mode = host.mode
    allowed = set(host.agent_allowlist or [])
    refused = []
    for action in sorted(set(action_types) & AGENT_ACTIONS):
        if action in _REPROVISION:
            ok = host.agent_allow_reprovision and mode != "monitor"
        elif mode == "monitor":
            ok = False
        elif mode == "full_control":
            ok = True
        elif action == "run_command":
            ok = False
        else:
            ok = action in allowed
        if not ok:
            refused.append(action)
    return refused


def task_action_types(parsed_spec: dict) -> set[str]:
    """Every action type a deploy of this spec asks the agent to run: each
    action's ``type`` plus each ``relevant:`` probe's type."""
    types = {a.get("type") for a in (parsed_spec or {}).get("actions") or [] if a.get("type")}

    def walk(node):
        if isinstance(node, dict):
            if "probe" in node and isinstance(node["probe"], dict):
                types.add(node["probe"].get("type"))
            for item in node.get("items") or []:
                walk(item)

    walk((parsed_spec or {}).get("relevant"))
    types.discard(None)
    return types


def refusals_for(hosts, parsed_spec: dict) -> list[dict]:
    """``[{"host_id", "hostname", "actions"}]`` for each host that would refuse."""
    needed = task_action_types(parsed_spec)
    out = []
    for host in hosts:
        actions = refused_actions(host, needed)
        if actions:
            out.append({"host_id": str(host.id), "hostname": host.hostname, "actions": actions})
    return out
