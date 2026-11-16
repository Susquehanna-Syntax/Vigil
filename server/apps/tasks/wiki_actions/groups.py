"""The group headings and one-line notes for the Built-in Actions page.

``apps/tasks/management/commands/render_wiki_actions.py`` reads these two
tables to write that page: ``GROUPS`` is the section order and membership,
``GROUP_NOTES`` the sentence under each heading.
"""

from __future__ import annotations

#: Group headings, in the order they appear on the page. Each names the
#: actions it holds; every action in the registry must appear exactly once,
#: which :meth:`_group_actions` enforces so a newly registered action cannot
#: be quietly left out of the page.
GROUPS: list[tuple[str, str, list[str]]] = [
    ("composition", "Composition", ["playbook"]),
    ("service", "Service management", [
        "restart_service", "start_service", "stop_service", "reload_service",
        "enable_service", "disable_service", "check_service",
    ]),
    ("container", "Container management", [
        "restart_container", "start_container", "stop_container", "pull_image",
        "recreate_container", "update_container", "remove_container",
        "docker_compose_up", "docker_compose_down", "clear_docker_logs",
        "check_docker_updates", "stack_restart", "stack_update", "container_logs",
        "stack_deploy", "stack_remove", "stack_read", "container_rollback",
        "docker_disk_usage", "image_remove", "image_prune", "volume_remove", "volume_prune",
        "network_remove", "network_prune",
    ]),
    ("files", "Files and directories", [
        "write_file", "create_directory", "delete_path", "copy_file",
        "move_file", "set_permissions",
    ]),
    ("packages", "Package management", [
        "install_package", "remove_package", "update_package",
        "run_package_updates",
    ]),
    ("apps", "Apps", [
        "app_inventory", "app_install", "app_upgrade", "app_uninstall",
        "app_pin", "app_install_custom", "app_ensure",
    ]),
    ("winupdate", "Windows Update", [
        "windows_update_scan", "windows_update_install",
    ]),
    ("system", "System", [
        "clear_temp_files", "execute_script", "reboot", "run_command",
        "set_hostname",
    ]),
    ("power", "Power", [
        "set_sleep_timeout", "set_display_timeout", "set_hibernate",
        "set_power_plan", "set_lid_action", "sleep_now", "hibernate_now",
    ]),
    ("network", "Networking", [
        "add_firewall_rule", "remove_firewall_rule", "list_firewall_rules",
        "set_firewall_policy", "enable_firewall", "disable_firewall",
    ]),
    ("users", "User management", [
        "create_user", "delete_user", "add_user_to_group",
    ]),
    ("cron", "Cron", ["create_cron_job", "delete_cron_job"]),
    ("agent", "Agent lifecycle", ["update_agent"]),
    ("reprovision", "Reprovisioning", [
        "reprovision_preflight", "reprovision_stage", "reprovision_commit",
        "reprovision_cleanup",
    ]),
    ("tags", "Host tagging", ["add_tag", "remove_tag"]),
    ("alerts", "Alerts and notifications", ["raise_alert", "resolve_alert",
                                            "send_notification"]),
    # ── Jackil helpdesk ─────────────────────────────────────────────────────
    #
    # A ticket is keyed by host + key, so a playbook that runs every night
    # finds last night's ticket instead of opening another. key, priority and
    # status come from the task the server signed; the free text comes from
    # the agent's report. With Jackil turned off the steps do nothing.
    ("jackil", "Jackil helpdesk", ["jackil_ticket", "jackil_note",
                                   "jackil_resolve"]),
    ("hunts", "Hunts", ["hunt_file", "hunt_package", "hunt_process", "hunt_port",
                        "hunt_service", "hunt_registry", "hunt_content"]),
    ("vuln", "Vulnerability scanning", [
        "request_nessus_scan", "request_network_scan", "run_trivy_scan",
        "trivy_db_update",
    ]),
]

#: One short sentence per group, shown under the heading.
GROUP_NOTES: dict[str, str] = {
    "composition": "Expanded by the server before the task is signed. The "
                   "agent never receives a playbook action, only the steps it "
                   "expands into.",
    "service": "Systemd on Linux, the Service Control Manager on Windows, "
               "launchd on macOS. Use the service name the host uses.",
    "container": "Docker only. The agent needs read and write access to the "
                 "Docker socket; without it these steps fail with a "
                 "permission error.",
    "files": "Paths are absolute. The agent refuses a relative path and "
             "refuses to escape its configured roots.",
    "packages": "apt, dnf, yum, zypper, pacman, winget, choco, or brew, "
                "whichever the host has. Use the package name that host's "
                "manager knows.",
    "apps": "Act on one app by the identity the Apps page shows — "
            "its source and package id. After a successful step the "
            "agent re-collects its software list, so the next "
            "check-in reports the change.",
    "winupdate": "Windows only. These drive the Windows Update COM API "
                 "directly rather than shelling out to a module.",
    "system": "The broadest actions, and most of the high-risk ones.",
    "power": "powercfg on Windows, logind on Linux, pmset on macOS. Where a "
             "platform has no equivalent the step fails with 'not supported' "
             "rather than reporting a change it did not make.",
    "network": "ufw, firewalld, nftables, or Windows Firewall, whichever the "
               "host runs. Ports 22 and 3389 cannot be closed by these "
               "actions.",
    "users": "Local accounts only. Vigil does not touch directory accounts.",
    "cron": "crontab on Linux and macOS. Not available on Windows.",
    "agent": "The agent replaces its own binary and restarts its service.",
    "reprovision": "A staged OS rebuild. Read docs/reprovisioning.md before "
                   "using any of these.",
    "tags": "Tags are server-side metadata about a host. The agent reports "
            "the step and the server applies the change from the definition "
            "it signed.",
    "alerts": "The agent reports the step and the server acts on the result, "
              "taking severity, key and channel from the task it signed — an "
              "agent can reword an alert but never choose its severity, its "
              "key or where it goes.",
    "jackil": "The agent reports the step and the server works the ticket, "
              "taking key, priority and status from the task it signed. A "
              "ticket belongs to one host and one key: run the playbook "
              "again and Vigil notes the open ticket instead of opening a "
              "second one, and once it is resolved the next failure opens a "
              "fresh ticket.",
    "vuln": "These leave a marker rather than doing the scan inline. The "
            "server starts the scan when the result arrives.",
    "hunts": "Read-only discovery: the walk runs in a low-priority thread "
             "with hard result and timeout caps, and scans a targeted scope "
             "by default — the whole disk only when you ask for it. Nothing "
             "on the host changes.",
}
