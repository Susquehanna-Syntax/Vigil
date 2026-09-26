"""Regenerate the wiki's Built-in Actions reference from ACTION_REGISTRY.

The reference used to be hand-maintained HTML, and it drifted: four actions
that do not exist were documented for months, and ``restart_service`` was
given a param name it does not take. A definition written from that page
failed validation on save, which is the worst place to find out the docs were
wrong.

This command writes the section instead. It reads ACTION_REGISTRY, builds one
disclosure per action with a complete task definition the reader can copy, and
replaces the block between the two marker comments in the wiki. Run it after
changing ACTION_REGISTRY:

    manage.py render_wiki_actions

Pass ``--check`` to fail instead of writing, which is what CI wants.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.tasks.spec import ACTION_REGISTRY, action_outputs

BEGIN = "<!-- BEGIN GENERATED: built-in actions (manage.py render_wiki_actions) -->"
END = "<!-- END GENERATED: built-in actions -->"

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
        "check_docker_updates",
    ]),
    ("files", "Files and directories", [
        "write_file", "create_directory", "delete_path", "copy_file",
        "move_file", "set_permissions",
    ]),
    ("packages", "Package management", [
        "install_package", "remove_package", "update_package",
        "run_package_updates",
    ]),
    ("winupdate", "Windows Update", [
        "windows_update_scan", "windows_update_install",
    ]),
    ("system", "System", [
        "clear_temp_files", "execute_script", "reboot", "run_command",
        "set_hostname",
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
    "winupdate": "Windows only. These drive the Windows Update COM API "
                 "directly rather than shelling out to a module.",
    "system": "The broadest actions, and most of the high-risk ones.",
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
    "vuln": "These leave a marker rather than doing the scan inline. The "
            "server starts the scan when the result arrives.",
    "hunts": "Read-only discovery: the walk runs in a low-priority thread "
             "with hard result and timeout caps, and scans a targeted scope "
             "by default — the whole disk only when you ask for it. Nothing "
             "on the host changes.",
}

#: Example value per param name, used to build each action's sample YAML.
#: Values are chosen to be obviously-an-example but still valid, so a reader
#: who copies one and forgets to edit it gets a clear failure rather than a
#: surprising success.
PARAM_EXAMPLES: dict[str, str] = {
    "action": "allow",
    "classifications": '"SecurityUpdates, CriticalUpdates"',
    "cmdline": '"auto=true priority=critical"',
    "command": '"systemctl is-active nginx"',
    "compose_file": "/opt/stacks/media/docker-compose.yml",
    "container_name": "nextcloud",
    "content": '"# managed by Vigil\\nmax_connections = 200\\n"',
    "defer_limit": "3",
    "defer_minutes": "60",
    "delay_seconds": "60",
    "dest": "/etc/nginx/nginx.conf",
    "direction": "incoming",
    "disk_target": "/dev/sda",
    "engine": "greenbone",
    "exclude_kb": '"KB5034441"',
    "expect": "active",
    "group": "docker",
    "groups": '"sudo, docker"',
    "hostname": "web-01",
    "image": "nginx:1.27-alpine",
    "include_kb": '"KB5032190"',
    "initrd_sha256": '"6f3c1e0d9a2b47c8e5d1f0a3b9c7e2d4a8f6b1c3e5d7a9f2b4c6e8d0a1f3b5c7"',
    "initrd_url": "https://vigil.example.com/images/debian-13/initrd.gz",
    "interface": "eth0",
    "job_id": '"${{ inputs.job_id }}"',
    "kernel_sha256": '"2a4c6e8d0b1f3a5c7e9d1b3f5a7c9e1d3b5f7a9c1e3d5b7f9a1c3e5d7b9f1a3c"',
    "kernel_url": "https://vigil.example.com/images/debian-13/vmlinuz",
    "log_lines": "200",
    "max_results": "50",
    "mode": '"0644"',
    "modified_within_days": "7",
    "name": '"Patch Tuesday rollout"',
    "notify": "true",
    "notify_message": '"Rebooting in 60 seconds for scheduled patching."',
    "older_than_days": "7",
    "os_family": "debian",
    "owner": "root",
    "package_name": "nginx",
    "path": "/etc/nginx/conf.d/vigil.conf",
    "paths": "/opt,/srv",
    "pattern": '"vigil-nightly-backup"',
    "platform": "linux",
    "policy": "deny",
    "port": "8443",
    "protocol": "tcp",
    "recursive": "false",
    "remove_home": "false",
    "rule_id": '"ufw-user-8443-tcp"',
    "schedule": '"0 3 * * *"',
    "scope": "os",
    "script_name": "healthcheck.sh",
    "security_only": "true",
    "service_name": "nginx",
    "services": '"web, worker"',
    "severity_floor": "important",
    "shell": "/bin/bash",
    "source": '"10.0.0.0/24"',
    "src": "/etc/nginx/nginx.conf.new",
    "tags": '"role:web, env:prod"',
    "timeout": "120",
    "user": "root",
    "username": "deploy",
}

#: What each param means, one line, shown in the param table.
PARAM_NOTES: dict[str, str] = {
    "action": "allow or deny. Defaults to allow.",
    "classifications": "Comma-separated update classifications to include.",
    "cmdline": "Kernel command line handed to the installer.",
    "command": "The command line to run. Quoted exactly as the shell sees it.",
    "compose_file": "Absolute path to the compose file on the host.",
    "container_name": "Container name as Docker reports it, not the image.",
    "content": "Full file body. Newlines as \\n.",
    "defer_limit": "How many times a user may defer before the step proceeds.",
    "defer_minutes": "How long each deferral postpones the step.",
    "delay_seconds": "Seconds to wait before the reboot fires.",
    "dest": "Absolute destination path.",
    "direction": "incoming or outgoing.",
    "disk_target": "Device the rebuild will write to.",
    "engine": "Which scanner to ask. Defaults to the configured one.",
    "exclude_kb": "Comma-separated KB numbers to skip.",
    "expect": "State to check for. The step fails if the service is not in it.",
    "group": "Existing group name.",
    "groups": "Comma-separated supplementary groups.",
    "hostname": "New hostname. Applied to the host and to its Vigil record.",
    "image": "Image reference including the tag.",
    "include_kb": "Comma-separated KB numbers to restrict the run to.",
    "initrd_sha256": "SHA-256 of the initrd. The agent refuses a mismatch.",
    "initrd_url": "Where the agent fetches the initrd from.",
    "interface": "Interface name to scope the rule to.",
    "job_id": "The reprovision job this step belongs to.",
    "kernel_sha256": "SHA-256 of the kernel. The agent refuses a mismatch.",
    "kernel_url": "Where the agent fetches the kernel from.",
    "mode": "Octal permissions, quoted so YAML keeps the leading zero.",
    "name": "Name of the playbook to expand.",
    "notify": "Warn the logged-in user before the step runs.",
    "notify_message": "What that warning says.",
    "older_than_days": "Only remove files last modified before this.",
    "os_family": "Installer family to stage.",
    "owner": "Owning user.",
    "package_name": "Package name as the host's package manager knows it.",
    "path": "Absolute path on the host.",
    "pattern": "Substring matched against existing cron lines.",
    "platform": "Force a platform build instead of detecting it.",
    "policy": "allow or deny, applied as the default for that direction.",
    "port": "Port number, or a low-high range.",
    "protocol": "tcp or udp.",
    "recursive": "Recurse into directories. Required to delete a non-empty one.",
    "remove_home": "Also delete the user's home directory.",
    "rule_id": "Backend rule identifier, from list_firewall_rules.",
    "schedule": "Five-field cron expression.",
    "scope": "os, fs, or config.",
    "script_name": "Filename of a script in the agent's script directory. "
                   "Give this or an inline body, not both.",
    "security_only": "Restrict the run to security updates.",
    "service_name": "Service name as the host's init system knows it.",
    "services": "Comma-separated services. Omit for the whole stack.",
    "severity_floor": "Lowest severity to include.",
    "shell": "Shell for an inline script body (bash, sh, powershell, pwsh), or "
             "the login shell for a new account.",
    "script": "Inline script body, run as-is with the shell you name. Inputs "
              "reach it as $VIGIL_INPUT_<ID> environment variables. A managed "
              "host runs it only after its owner approves its hash.",
    "source": "Source address or CIDR the rule applies to.",
    "src": "Absolute source path.",
    "tags": "Comma-separated tags. A string, not a list.",
    "timeout": "Seconds before the agent kills the command.",
    "user": "Account whose crontab is edited. Defaults to root.",
    "username": "Local account name.",
    "hash": "Include each match's sha256 (always on when sha256 is given).",
    "max_results": "Stop after this many matches (default 500, at most 5000).",
    "max_size": "Only files at most this many bytes.",
    "min_size": "Only files at least this many bytes.",
    "modified_within_days": "Only files modified within this many days.",
    "paths": "Comma-separated roots to walk instead of the scope.",
    "sha256": "Only files with exactly this sha256 (64 hex characters).",
    "manager": "Package manager to ask (dpkg, rpm, pacman, brew, snap). Detected when omitted.",
    "version_eq": "Only this exact version, compared with the package system's own version rules.",
    "version_gt": "Only versions newer than this, compared with the package system's own version rules.",
    "version_gte": "Only this version or newer, compared with the package system's own version rules.",
    "version_lt": "Only versions older than this, compared with the package system's own version rules.",
    "version_lte": "Only this version or older, compared with the package system's own version rules.",
    "process": "Glob on the owning process name (hunt_port).",
    "state": "running or stopped (hunt_service).",
    "start_mode": "enabled or disabled (hunt_service).",
}

#: Per-action framing for the sample definition: the task name, and a sentence
#: saying what it does. Falls back to the registry label.
EXAMPLE_TITLES: dict[str, tuple[str, str]] = {
    "playbook": ("Run the patching playbook",
                 "Expand a playbook by name and run every step in it."),
    "restart_service": ("Restart nginx and confirm it came back",
                        "Restart the service, then check it is active again."),
    "check_service": ("Check that nginx is running",
                      "Read-only. Reports whether the service is active; fails the step only when expect is set and the service is not in that state."),
    "recreate_container": ("Update Nextcloud to a new image",
                           "Pull the image, then recreate the container on it."),
    "write_file": ("Deploy a config file",
                   "Write the file, then reload the service that reads it."),
    "delete_path": ("Remove a stale build directory",
                    "Deletes recursively. There is no undo."),
    "install_package": ("Install nginx",
                        "Install the package, then enable it at boot."),
    "run_package_updates": ("Apply security updates",
                            "Run the host's package manager, security only."),
    "windows_update_scan": ("Scan for Windows security updates",
                            "Read-only. Reports what is missing without installing."),
    "windows_update_install": ("Install Windows security updates",
                               "Scan first, then install what the scan found."),
    "execute_script": ("Run an allowlisted script",
                       ("Give a script_name to run a file from the agent's "
                        "script directory, or a shell and an inline script "
                        "body to run that exact body: a managed host runs an "
                        "inline body only after its owner approves its hash.")),
    "reboot": ("Reboot after patching",
               "Warns the logged-in user, then reboots after a delay."),
    "run_command": ("Run a one-off command",
                    "Highest-risk action. Managed-mode agents refuse it."),
    "add_firewall_rule": ("Open a port to one subnet",
                          "Adds an allow rule scoped to a source CIDR."),
    "create_user": ("Create a deploy account",
                    "Creates the account and adds it to the groups you name."),
    "create_cron_job": ("Schedule a nightly job",
                        "Writes one crontab line for the user you name."),
    "update_agent": ("Update the Vigil agent",
                     "The agent replaces its binary and restarts its service."),
    "add_tag": ("Tag Docker hosts automatically",
                "Runs only where the when: predicate is true on the host."),
    "remove_tag": ("Drop a tag once a host is patched",
                   "Tags are applied server-side from the definition it signed."),
    "run_trivy_scan": ("Scan the host filesystem with Trivy",
                       "Leaves a marker. The scan starts when the result arrives."),
    "hunt_file": ("Hunt for a jar on disk",
                  "Walks the targeted scope and lists every file whose name "
                  "matches the glob — here, the Log4Shell-era core jar."),
    "hunt_package": ("Find hosts with an old openssl",
                     "Lists installed packages named openssl older than 3.0.13, "
                     "using the host's own package version rules."),
    "hunt_process": ("Find hosts running java",
                     "Lists running processes named java, with pid, command line and user."),
    "hunt_port": ("Find hosts listening on 8443",
                  "Lists local TCP/UDP socket bindings on port 8443, with the owning process."),
    "hunt_service": ("Find hosts with ssh enabled but stopped",
                     "Lists services named ssh that are enabled but currently stopped."),
    "hunt_registry": ("Find hosts with Java in their Uninstall keys",
                      "Windows only. Reads the registry for keys and values — "
                      "here, the DisplayName of every installed product in "
                      "the 64-bit Uninstall tree, filtered to Java."),
    "hunt_content": ("Find hosts with a leaked token in a config",
                     "Scans file contents for a regex. By default a match "
                     "reports which files matched and on which lines; "
                     "return: text also carries the matched substrings, "
                     "which makes the step high risk and is audit-logged on "
                     "Business."),
}


#: What each declared output means (action -> field -> sentence). Every output
#: an action declares must have a note here, or the render fails — an output a
#: reader cannot understand is one nobody will branch on correctly.
#: Notes for a param whose meaning differs by action (the same name means
#: something else elsewhere: "name" is a playbook's name, "scope" a Trivy scope).
ACTION_PARAM_NOTES: dict[tuple[str, str], str] = {
    ("hunt_file", "name"): "Exact file name, or a glob (* ?) matched against the base name.",
    ("hunt_file", "scope"): "targeted (default: install and home directories) or full (every filesystem root).",
    ("hunt_file", "older_than_days"): "Only files last modified more than this many days ago.",
    ("hunt_package", "name"): "Package name, or a glob (* ?).",
    ("hunt_package", "timeout"): "Seconds before the hunt stops and returns what it found (default 120, at most 600).",
    ("hunt_file", "timeout"): "Seconds before the hunt stops and returns what it found (default 120, at most 600).",
    ("hunt_file", "version_lt"): "Only files whose version is older than this, compared with dotted-numeric rules. The file's version: JAR manifest, Windows version resource, or the version in the file name. Files with no readable version never match.",
    ("hunt_file", "version_lte"): "Only files whose version is this or older, compared with dotted-numeric rules. The file's version: JAR manifest, Windows version resource, or the version in the file name. Files with no readable version never match.",
    ("hunt_file", "version_gt"): "Only files whose version is newer than this, compared with dotted-numeric rules. The file's version: JAR manifest, Windows version resource, or the version in the file name. Files with no readable version never match.",
    ("hunt_file", "version_gte"): "Only files whose version is this or newer, compared with dotted-numeric rules. The file's version: JAR manifest, Windows version resource, or the version in the file name. Files with no readable version never match.",
    ("hunt_file", "version_eq"): "Only files whose version equals this, compared with dotted-numeric rules. The file's version: JAR manifest, Windows version resource, or the version in the file name. Files with no readable version never match.",

    ("hunt_process", "name"): "Process name to match as a glob, e.g. \"java\" or \"sshd*\".",
    ("hunt_process", "cmdline"): "Case-insensitive substring of the joined command line.",
    ("hunt_process", "user"): "Exact username the process runs as.",
    ("hunt_process", "timeout"): "Seconds before the process walk stops early (default 120, at most 600).",

    ("hunt_port", "port"): "Port number, or a low-high range.",
    ("hunt_port", "protocol"): "tcp or udp; omitted means both.",
    ("hunt_port", "process"): "Glob on the owning process name.",
    ("hunt_port", "timeout"): "Seconds before the socket walk stops early (default 120, at most 600).",

    ("hunt_service", "name"): "Service name to match as a glob (the .service suffix is not required).",
    ("hunt_service", "timeout"): "Seconds before the service walk stops early (default 120, at most 600).",
    ("hunt_registry", "key"): "Registry key path with an HKLM, HKCU or HKU root; a final wildcard segment adds each direct subkey. Single-quote it in YAML so the backslashes stay literal.",
    ("hunt_registry", "value"): "Glob on value names; when absent the matching keys themselves are reported.",
    ("hunt_registry", "timeout"): "Seconds before the walk stops early (default 120, at most 600).",
    ("hunt_content", "name"): "File-name glob matched against the base name (default *).",
    ("hunt_content", "pattern"): "Regular expression searched inside file contents.",
    ("hunt_content", "paths"): "Comma-separated roots to walk instead of the scope.",
    ("hunt_content", "scope"): "targeted (default: install and home directories) or full (every filesystem root).",
    ("hunt_content", "timeout"): "Seconds before the walk stops early (default 120, at most 600).",

    ("hunt_file", "stays_open"): "How long the hunt waits for hosts that never check in, before they show as did not report (default 7 days; '<n>m', '<n>h', '<n>d' or seconds; 1 hour to 30 days).",
    ("hunt_package", "stays_open"): "How long the hunt waits for hosts that never check in, before they show as did not report (default 7 days; '<n>m', '<n>h', '<n>d' or seconds; 1 hour to 30 days).",
    ("hunt_process", "stays_open"): "How long the hunt waits for hosts that never check in, before they show as did not report (default 7 days; '<n>m', '<n>h', '<n>d' or seconds; 1 hour to 30 days).",
    ("hunt_port", "stays_open"): "How long the hunt waits for hosts that never check in, before they show as did not report (default 7 days; '<n>m', '<n>h', '<n>d' or seconds; 1 hour to 30 days).",
    ("hunt_service", "stays_open"): "How long the hunt waits for hosts that never check in, before they show as did not report (default 7 days; '<n>m', '<n>h', '<n>d' or seconds; 1 hour to 30 days).",
    ("hunt_registry", "stays_open"): "How long the hunt waits for hosts that never check in, before they show as did not report (default 7 days; '<n>m', '<n>h', '<n>d' or seconds; 1 hour to 30 days).",
    ("hunt_content", "stays_open"): "How long the hunt waits for hosts that never check in, before they show as did not report (default 7 days; '<n>m', '<n>h', '<n>d' or seconds; 1 hour to 30 days).",
}


OUTPUT_NOTES: dict[str, dict[str, str]] = {
    "check_service": {
        "active": "True when systemd reports the unit active.",
        "state": "The raw systemctl is-active word (active, inactive, failed, …).",
    },
    "update_container": {
        "updated": "True when the container now runs a different image than before.",
        "old_image_id": "Image id the container ran before the update.",
        "new_image_id": "Image id the container runs now.",
    },
    "check_docker_updates": {
        "checked": "How many Docker Hub-tagged containers were checked.",
        "outdated": "How many of them have a newer image available.",
    },
    "run_command": {
        "exit_code": "The command's exit code (0 — a non-zero exit fails the step).",
    },
    "restart_service": {"active": "True when systemd reports the unit active after the restart."},
    "start_service": {"active": "True when systemd reports the unit active after the start."},
    "stop_service": {"active": "True when the unit is still active after the stop (normally false)."},
    "reload_service": {"active": "True when systemd reports the unit active after the reload."},
    "enable_service": {"enabled": "True when systemd now reports the unit enabled."},
    "disable_service": {"enabled": "True when the unit is still enabled (normally false)."},
    "restart_container": {"running": "True when the container is running after the restart."},
    "start_container": {"running": "True when the container is running after the start."},
    "stop_container": {"running": "True when the container is still running (normally false)."},
    "pull_image": {"image_id": "Id of the image the reference now points to, after the pull."},
    "remove_container": {"removed": "True once the container has been removed."},
    "recreate_container": {
        "updated": "True when the recreated container runs a different image.",
        "old_image_id": "Image id before the recreate.",
        "new_image_id": "Image id after the recreate.",
    },
    "docker_compose_up": {"compose_file": "The compose file that was brought up."},
    "docker_compose_down": {"compose_file": "The compose file that was taken down."},
    "clear_docker_logs": {"truncated": "True when a log file was found and emptied."},
    "write_file": {"path": "The file written.", "bytes": "How many characters were written."},
    "create_directory": {"path": "The directory created."},
    "delete_path": {"path": "The path deleted.", "recursive": "True when a directory tree was removed."},
    "copy_file": {"src": "The source path.", "dest": "The destination path."},
    "move_file": {"src": "The source path.", "dest": "The destination path."},
    "set_permissions": {"path": "The path whose mode or owner was set."},
    "install_package": {
        "package": "The package name as given.",
        "manager": "The package manager used (apt-get, dnf, winget, …).",
        "installed_version": "Version now installed; empty when this manager is not queried (apk, winget) or the query failed.",
    },
    "windows_update_scan": {"count": "How many updates matched the filters."},
    "windows_update_install": {
        "installed_count": "How many updates the installer reported as installed.",
        "failed_count": "How many selected updates failed to install.",
        "reboot_required": "True when a reboot is needed to finish applying the updates (the agent never reboots on its own).",
    },
    "update_agent": {"version": "The agent version now installed."},
    "add_tag": {"tags": "The tags requested (comma-separated)."},
    "remove_tag": {"tags": "The tags requested for removal (comma-separated)."},
    "request_nessus_scan": {"requested": "Always true once the request is recorded."},
    "request_network_scan": {"engine": "The scan engine requested, or auto when the server picks."},
    "run_trivy_scan": {"vulnerabilities": "Total findings across the report; -1 when the report could not be parsed."},
    "trivy_db_update": {"updated": "Always true once the database update finished."},
    "hunt_file": {
        "matched": "True when at least one file matched.",
        "count": "How many matches were returned (the cap when truncated).",
        "truncated": "True when the walk stopped early at max_results or the timeout, and more matches exist.",
    },
    "hunt_package": {
        "matched": "True when at least one installed package matched.",
        "count": "How many packages matched (the cap when truncated).",
        "truncated": "True when the listing stopped early at max_results or the timeout.",
    },
    "hunt_process": {
        "matched": "True when at least one running process matched.",
        "count": "How many processes matched (the cap when truncated).",
        "truncated": "True when the process walk stopped early at max_results or the timeout.",
    },
    "hunt_port": {
        "matched": "True when at least one local socket binding matched.",
        "count": "How many bindings matched (the cap when truncated).",
        "truncated": "True when the socket walk stopped early at max_results or the timeout.",
    },
    "hunt_service": {
        "matched": "True when at least one service matched.",
        "count": "How many services matched (the cap when truncated).",
        "truncated": "True when the service walk stopped early at max_results or the timeout.",
    },
    "hunt_registry": {
        "matched": "True when at least one key or value matched.",
        "count": "How many keys and values matched (the cap when truncated).",
        "truncated": "True when the walk stopped early at max_results or the timeout.",
    },
    "hunt_content": {
        "matched": "True when at least one file matched.",
        "count": "How many files matched (the cap when truncated).",
        "truncated": "True when the walk stopped early at max_results or the timeout.",
    },
    "update_package": {
        "package": "The package name as given.",
        "manager": "The package manager used.",
        "installed_version": "Version now installed; empty when unknown.",
    },
    "remove_package": {"package": "The package name as given.", "manager": "The package manager used."},
    "run_package_updates": {"manager": "The package manager used.", "security_only": "True when only security updates were requested."},
    "clear_temp_files": {"removed": "Files deleted.", "skipped": "Files left because they were in use or not permitted."},
    "reboot": {"delay_seconds": "Seconds until the reboot.", "deferral_active": "True when a logged-in user may still defer it."},
    "set_hostname": {"hostname": "The hostname now set."},
    "add_firewall_rule": {"port": "The rule's port.", "protocol": "The rule's protocol.", "action": "allow or deny."},
    "remove_firewall_rule": {"port": "The rule's port.", "protocol": "The rule's protocol.", "action": "allow or deny."},
    "list_firewall_rules": {
        "supported": "False when the host has no supported firewall tool.",
        "enabled": "True when the firewall is on.",
        "rule_count": "How many rules were read.",
    },
    "set_firewall_policy": {"direction": "incoming or outgoing.", "policy": "The default policy now set."},
    "enable_firewall": {"enabled": "Always true after this step."},
    "disable_firewall": {"enabled": "Always false after this step."},
    "create_user": {"username": "The account created."},
    "delete_user": {"username": "The account deleted."},
    "add_user_to_group": {"username": "The account.", "group": "The group it joined."},
    "create_cron_job": {"user": "Whose crontab gained the line."},
    "delete_cron_job": {"user": "Whose crontab was edited.", "removed": "How many lines matched and were removed (0 when none)."},
    "execute_script": {
        "exit_code": "The script's exit code (0 — a non-zero exit fails the step).",
    },
}


def _check_output_notes() -> None:
    missing = [f"{action}.{field}" for action in ACTION_REGISTRY
               for field in action_outputs(action)
               if field not in OUTPUT_NOTES.get(action, {})]
    if missing:
        raise CommandError(
            "declared outputs with no entry in OUTPUT_NOTES: "
            + ", ".join(sorted(missing)))


def _group_actions() -> list[tuple[str, str, list[str]]]:
    """GROUPS, checked against the registry.

    A new action that nobody added to GROUPS would otherwise vanish from the
    page while every existing test still passed, which is the exact failure
    this file exists to prevent.
    """
    placed = [name for _, _, names in GROUPS for name in names]
    duplicated = {n for n in placed if placed.count(n) > 1}
    if duplicated:
        raise CommandError(f"listed in two groups: {sorted(duplicated)}")
    missing = sorted(set(ACTION_REGISTRY) - set(placed))
    if missing:
        raise CommandError(
            f"not in any group in render_wiki_actions.GROUPS: {missing}. "
            "Add them to a group so they appear in the wiki.")
    unknown = sorted(set(placed) - set(ACTION_REGISTRY))
    if unknown:
        raise CommandError(f"grouped but not in ACTION_REGISTRY: {unknown}")
    _check_output_notes()
    return GROUPS


#: Params whose example value is a `${{ inputs.x }}` reference. The reference
#: only validates if the input is also declared, so the example declares it.
EXAMPLE_INPUTS: dict[str, list[str]] = {
    "job_id": [
        "inputs:",
        "  - id: job_id",
        "    label: Reprovision job ID",
        "    description: The job this step belongs to.",
        "    type: text",
    ],
}


def example_yaml(action: str) -> str:
    """A complete, valid task definition using this action.

    Complete matters: a fragment showing only the params cannot be pasted into
    the editor, and a reader who pastes one gets a validation error for the
    missing name and actions list rather than a working task. Every example
    this function returns is run through ``parse_and_validate`` by the tests.
    """
    entry = ACTION_REGISTRY[action]
    title, _ = EXAMPLE_TITLES.get(action, (entry["label"], ""))
    params = list(entry["required"])
    if not params and action == "execute_script":
        # `execute_script` accepts exactly one of script_name or an inline
        # body; the example shows the allowlisted-file form.
        params = ["script_name"]
    if action == "hunt_package":
        # "name" here is the package, not the task, so the example is written out.
        return "\n".join([
            "name: Find hosts with an old openssl",
            "description: \"Hunt for installed packages, on the hosts you dispatch this to.\"",
            "risk: low",
            "actions:",
            "  - id: hunt-package",
            "    type: hunt_package",
            "    params:",
            "      name: openssl",
            '      version_lt: "3.0.13"',
        ]) + "\n"
    if action == "hunt_file":
        # `hunt_file` takes no *required* params but refuses to run without
        # a name or a sha256; the example shows the glob form. The param's
        # example lives under its own key because "name" is the task's name.
        lines = [
            "name: Hunt for a known-bad jar",
            "description: \"Hunt for files on disk, on the hosts you dispatch this to.\"",
            "risk: low",
            "actions:",
            "  - id: hunt-file",
            "    type: hunt_file",
            "    params:",
            '      name: "log4j-core-2.1*.jar"',
            "      paths: /opt,/srv",
            "      max_results: 50",
        ]
        return "\n".join(lines) + "\n"
    if action == "hunt_process":
        # `hunt_process` takes no *required* params but refuses to run without
        # a name, a cmdline or a user; the example shows the name-glob form.
        return "\n".join([
            "name: Find hosts running java",
            "description: \"Hunt for running processes, on the hosts you dispatch this to.\"",
            "risk: low",
            "actions:",
            "  - id: hunt-process",
            "    type: hunt_process",
            "    params:",
            '      name: "java"',
            "      max_results: 50",
        ]) + "\n"
    if action == "hunt_port":
        # `hunt_port` takes no *required* params but refuses to run without
        # a port or a process; the example shows the port form.
        return "\n".join([
            "name: Find hosts listening on 8443",
            "description: \"Hunt for local socket bindings, on the hosts you dispatch this to.\"",
            "risk: low",
            "actions:",
            "  - id: hunt-port",
            "    type: hunt_port",
            "    params:",
            "      port: 8443",
            "      protocol: tcp",
            "      max_results: 50",
        ]) + "\n"
    if action == "hunt_service":
        # `name` here is the service, not the task, so the example is written out.
        return "\n".join([
            "name: Find hosts with ssh enabled but stopped",
            "description: \"Hunt for services, on the hosts you dispatch this to.\"",
            "risk: low",
            "actions:",
            "  - id: hunt-service",
            "    type: hunt_service",
            "    params:",
            "      name: ssh",
            "      state: stopped",
            "      start_mode: enabled",
            "      max_results: 50",
        ]) + "\n"
    if action == "hunt_registry":
        # `key` carries the root and the wildcard; the example filters the
        # DisplayName values of the 64-bit Uninstall tree to Java.
        return "\n".join([
            "name: Find hosts with Java in their Uninstall keys",
            "description: \"Hunt the Windows registry, on the hosts you dispatch this to.\"",
            "risk: low",
            "actions:",
            "  - id: hunt-registry",
            "    type: hunt_registry",
            "    params:",
            "      key: 'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'",
            "      value: DisplayName",
            "      data: Java",
            "      view: 64",
            "      max_results: 50",
        ]) + "\n"
    if action == "hunt_content":
        # The example keeps the default return (lines) — return: text makes
        # the step high risk, which a low-risk example must not claim.
        return "\n".join([
            "name: Find hosts with a leaked token in a config",
            "description: \"Hunt for text inside files, on the hosts you dispatch this to.\"",
            "risk: standard",
            "actions:",
            "  - id: hunt-content",
            "    type: hunt_content",
            "    params:",
            '      pattern: "AKIA[0-9A-Z]{16}"',
            "      name: \"*.conf\"",
            "      paths: /etc,/opt",
            "      return: lines",
            "      max_results: 50",
        ]) + "\n"
    lines = [
        f"name: {title}",
        f"description: {entry['label']} on the hosts you dispatch this to.",
        f"risk: {entry['risk']}",
    ]
    for param in params:
        lines.extend(EXAMPLE_INPUTS.get(param, []))
    lines.append("actions:")
    lines.append(f"  - id: {action.replace('_', '-')}")
    lines.append(f"    type: {action}")
    if params:
        lines.append("    params:")
        for param in params:
            lines.append(f"      {param}: {PARAM_EXAMPLES[param]}")
    return "\n".join(lines) + "\n"


def _yaml_html(source: str) -> str:
    """YAML with the wiki's key/string highlighting spans."""
    out = []
    for line in source.rstrip("\n").split("\n"):
        match = re.match(r"^(\s*)(- )?([A-Za-z_][\w]*): ?(.*)$", line)
        if not match:
            out.append(html.escape(line))
            continue
        indent, dash, key, value = match.groups()
        piece = indent + (dash or "")
        piece += f'<span class="hl-key">{html.escape(key)}</span>: '
        if value:
            piece += f'<span class="hl-string">{html.escape(value)}</span>'
        out.append(piece.rstrip())
    return "\n".join(out)


def _params_table(entry: dict, action: str = "") -> str:
    def note(param: str) -> str:
        return ACTION_PARAM_NOTES.get((action, param), PARAM_NOTES.get(param, ""))

    rows = []
    for param in entry["required"]:
        rows.append(
            f'<tr><td><code>{param}</code></td><td><span class="param-req">'
            f'required</span></td><td>{html.escape(note(param))}'
            f"</td></tr>")
    for param in entry["optional"]:
        rows.append(
            f'<tr><td><code>{param}</code></td><td><span class="param-opt">'
            f'optional</span></td><td>{html.escape(note(param))}'
            f"</td></tr>")
    if not rows:
        return '<p class="action-noparams">This action takes no params.</p>'
    return (
        '<div class="table-wrap"><table class="param-table">'
        "<thead><tr><th>Param</th><th></th><th>Meaning</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")


def _outputs_table(action: str) -> str:
    """What a later step can read from this one: status always, plus outputs."""
    outputs = action_outputs(action)
    rows = [
        '<tr><td><code>status</code></td><td>str</td>'
        "<td>ok or skipped, as a later step sees it (a failed step stops the task).</td></tr>"]
    for field, kind in outputs.items():
        rows.append(
            f"<tr><td><code>{html.escape(field)}</code></td><td>{html.escape(kind)}</td>"
            f"<td>{html.escape(OUTPUT_NOTES[action][field])}</td></tr>")
    table = (
        '<div class="table-wrap"><table class="param-table">'
        "<thead><tr><th>Output</th><th>Type</th><th>Meaning</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")
    if outputs:
        table += ('<p class="prose">Read one later as '
                  "<code>${{ steps.&lt;id&gt;.result.&lt;field&gt; }}</code>.</p>")
    return table


def render() -> str:
    parts = [BEGIN]
    parts.append(
        '<div class="action-toolbar">\n'
        '  <input type="search" id="action-filter" class="action-search" '
        'placeholder="Filter actions by name or param" '
        'aria-label="Filter actions">\n'
        '  <select id="action-group" class="action-select" '
        'aria-label="Filter by category">\n'
        '    <option value="">All categories</option>\n'
        + "".join(
            f'    <option value="{slug}">{html.escape(title)}</option>\n'
            for slug, title, _ in GROUPS)
        + '  </select>\n'
        '  <select id="action-risk" class="action-select" '
        'aria-label="Filter by risk">\n'
        '    <option value="">All risk tiers</option>\n'
        '    <option value="low">Low risk</option>\n'
        '    <option value="standard">Standard risk</option>\n'
        '    <option value="high">High risk</option>\n'
        '  </select>\n'
        '</div>\n'
        '<p class="action-count" id="action-count"></p>')

    for slug, title, names in _group_actions():
        parts.append(f'<div class="action-group" data-group="{slug}">')
        parts.append(f'<h4 class="action-group-title">{html.escape(title)}</h4>')
        note = GROUP_NOTES.get(slug)
        if note:
            parts.append(f'<p class="action-group-note">{html.escape(note)}</p>')
        for action in names:
            entry = ACTION_REGISTRY[action]
            risk = entry["risk"]
            risk_class = {"low": "risk-low", "standard": "risk-std",
                          "high": "risk-high"}[risk]
            _, blurb = EXAMPLE_TITLES.get(action, (None, ""))
            keywords = " ".join([action] + entry["required"] + entry["optional"])
            required = ", ".join(entry["required"]) or "none"
            parts.append(
                f'<details class="action" data-group="{slug}" '
                f'data-risk="{risk}" data-keywords="{html.escape(keywords)}">'
                f"<summary>"
                f'<code class="action-name">{action}</code>'
                f'<span class="risk {risk_class}">{risk}</span>'
                f'<span class="action-label">{html.escape(entry["label"])}</span>'
                f'<span class="action-required">{html.escape(required)}</span>'
                f"</summary>"
                f'<div class="action-body">')
            if blurb:
                parts.append(f'<p class="prose">{html.escape(blurb)}</p>')
            parts.append(_params_table(entry, action))
            parts.append(_outputs_table(action))
            parts.append(
                '<div class="code-block"><div class="code-label">YAML</div>'
                '<button class="copy-btn" type="button">Copy</button>'
                f"<pre>{_yaml_html(example_yaml(action))}</pre></div>")
            parts.append("</div></details>")
        parts.append("</div>")

    parts.append('<p class="action-empty" id="action-empty" hidden>'
                 "No action matches that filter.</p>")
    parts.append(END)
    return "\n".join(parts)


class Command(BaseCommand):
    help = ("Regenerate the wiki's Built-in Actions reference from "
            "ACTION_REGISTRY. --check fails instead of writing.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true",
            help="Exit non-zero if the wiki is out of date, and write nothing.")

    def handle(self, *args, **opts):
        path = Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html"
        if not path.is_file():
            raise CommandError(f"wiki not found at {path}")
        text = path.read_text(encoding="utf-8")
        if BEGIN not in text or END not in text:
            raise CommandError(
                "marker comments missing from the wiki. The generated block is "
                f"delimited by:\n  {BEGIN}\n  {END}")

        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        updated = head + render() + tail

        if updated == text:
            self.stdout.write(self.style.SUCCESS("Wiki action reference is up to date."))
            return
        if opts["check"]:
            raise CommandError(
                "wiki action reference is out of date. Run:\n"
                "  manage.py render_wiki_actions")
        path.write_text(updated, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(ACTION_REGISTRY)} actions to {path}"))
