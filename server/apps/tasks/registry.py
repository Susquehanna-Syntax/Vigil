"""The action registry: every action a task can run, its params, risk and outputs.

Split out of ``spec.py`` (which validates and resolves task YAML) so the data
that every new action touches lives in one small file. ``spec.py`` re-exports
everything here, so ``from apps.tasks.spec import ACTION_REGISTRY`` keeps working.
"""

from __future__ import annotations

from typing import Any

# ── Action registry ──────────────────────────────────────────────────────────
#
# Keep this list in lockstep with ``agent/vigil_agent/executor.py``. Each entry
# records required params, a risk tier, and a human label for the UI.

#: Action types that were renamed. A community file published before the rename,
#: and any YAML an operator saved locally, keeps working: the old name resolves
#: to the new one on the way in and is rewritten in place, so everything
#: downstream — validation, expansion, export — only ever sees the new name.
LEGACY_ACTION_ALIASES: dict[str, str] = {
    "baseline": "playbook",
}

ACTION_REGISTRY: dict[str, dict[str, Any]] = {
    # ── Composition (server-side; expanded before signing, never sent to agents) ──
    "playbook": {
        "label": "Run playbook",
        "risk": "standard",
        "required": ["name"],
        "optional": [],
        "outputs": {},  # expanded server-side, never executed by the agent
    },
    # ── Service management ──────────────────────────────────────────────────
    "restart_service": {
        "label": "Restart service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
        "outputs": {"active": "bool"},
    },
    "start_service": {
        "label": "Start service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
        "outputs": {"active": "bool"},
    },
    "stop_service": {
        "label": "Stop service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
        "outputs": {"active": "bool"},
    },
    "reload_service": {
        "label": "Reload service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
        "outputs": {"active": "bool"},
    },
    "enable_service": {
        "label": "Enable service",
        "risk": "low",
        "required": ["service_name"],
        "optional": [],
        "outputs": {"enabled": "bool"},
    },
    "disable_service": {
        "label": "Disable service",
        "risk": "standard",
        "required": ["service_name"],
        "optional": [],
        "outputs": {"enabled": "bool"},
    },
    "check_service": {
        "label": "Check service status",
        "risk": "low",
        "required": ["service_name"],
        "optional": ["expect"],
        "outputs": {"active": "bool", "state": "str"},
    },
    # ── Container management ────────────────────────────────────────────────
    "restart_container": {
        "label": "Restart container",
        "risk": "standard",
        "required": ["container_name"],
        "optional": [],
        "outputs": {"running": "bool"},
    },
    "start_container": {
        "label": "Start container",
        "risk": "low",
        "required": ["container_name"],
        "optional": [],
        "outputs": {"running": "bool"},
    },
    "stop_container": {
        "label": "Stop container",
        "risk": "standard",
        "required": ["container_name"],
        "optional": [],
        "outputs": {"running": "bool"},
    },
    "pull_image": {
        "label": "Pull container image",
        "risk": "low",
        "required": ["image"],
        "optional": [],
        "outputs": {"image_id": "str"},
    },
    # Applies a freshly pulled image: docker restart alone keeps the container
    # on its original image, so image updates require recreation. The agent
    # carries the container's user-supplied config over and rolls back to the
    # original container if the replacement fails to start.
    "recreate_container": {
        "label": "Recreate container (apply new image)",
        "risk": "standard",
        "required": ["container_name"],
        "optional": ["image"],
        "outputs": {"updated": "bool", "old_image_id": "str", "new_image_id": "str"},
    },
    "update_container": {
        "label": "Update container (pull + apply new image)",
        "risk": "standard",
        "required": ["container_name"],
        "optional": [],
        "outputs": {"updated": "bool", "old_image_id": "str", "new_image_id": "str"},
    },
    "remove_container": {
        "label": "Remove container",
        "risk": "high",
        "required": ["container_name"],
        "optional": [],
        "outputs": {"removed": "bool"},
    },
    "docker_compose_up": {
        "label": "Docker Compose up",
        "risk": "standard",
        "required": ["compose_file"],
        "optional": ["services"],  # comma-separated service names
        "outputs": {"compose_file": "str"},
    },
    "docker_compose_down": {
        "label": "Docker Compose down",
        "risk": "standard",
        "required": ["compose_file"],
        "optional": [],
        "outputs": {"compose_file": "str"},
    },
    "clear_docker_logs": {
        "label": "Truncate Docker logs",
        "risk": "low",
        "required": [],
        "optional": ["container_name"],
        "outputs": {"truncated": "bool"},
    },
    # Forces an immediate Docker Hub digest re-check instead of waiting out
    # the agent's docker_check_interval — outdated-image alerts fire or
    # resolve on the next check-in (~1 min). Read-only: HEAD requests only,
    # never a pull.
    "check_docker_updates": {
        "label": "Check Docker images for updates now",
        "risk": "low",
        "required": [],
        "optional": [],
        "outputs": {"checked": "int", "outdated": "int"},
    },
    # ── File / directory operations ─────────────────────────────────────────
    "write_file": {
        "label": "Write file",
        "risk": "high",
        "required": ["path", "content"],
        "optional": ["mode"],
        "outputs": {"path": "str", "bytes": "int"},
    },
    "create_directory": {
        "label": "Create directory",
        "risk": "low",
        "required": ["path"],
        "optional": ["owner", "group", "mode"],
        "outputs": {"path": "str"},
    },
    "delete_path": {
        "label": "Delete path",
        "risk": "high",
        "required": ["path"],
        "optional": ["recursive"],
        "outputs": {"path": "str", "recursive": "bool"},
    },
    "copy_file": {
        "label": "Copy file",
        "risk": "standard",
        "required": ["src", "dest"],
        "optional": [],
        "outputs": {"src": "str", "dest": "str"},
    },
    "move_file": {
        "label": "Move file",
        "risk": "standard",
        "required": ["src", "dest"],
        "optional": [],
        "outputs": {"src": "str", "dest": "str"},
    },
    "set_permissions": {
        "label": "Set permissions",
        "risk": "standard",
        "required": ["path"],
        "optional": ["owner", "group", "mode"],
        "outputs": {"path": "str"},
    },
    # ── Package management ──────────────────────────────────────────────────
    "install_package": {
        "label": "Install package",
        "risk": "standard",
        "required": ["package_name"],
        "optional": [],
        "outputs": {"package": "str", "manager": "str", "installed_version": "str"},
    },
    "remove_package": {
        "label": "Remove package",
        "risk": "standard",
        "required": ["package_name"],
        "optional": [],
        "outputs": {"package": "str", "manager": "str"},
    },
    "update_package": {
        "label": "Update package",
        "risk": "standard",
        "required": ["package_name"],
        "optional": [],
        "outputs": {"package": "str", "manager": "str", "installed_version": "str"},
    },
    "run_package_updates": {
        "label": "Run system updates",
        "risk": "standard",
        "required": [],
        "optional": ["security_only"],
        "outputs": {"manager": "str", "security_only": "bool"},
    },
    # ── Windows Update ──────────────────────────────────────────────────────
    "windows_update_scan": {
        "label": "Scan for Windows updates",
        "risk": "low",
        "required": [],
        "optional": ["classifications", "include_kb", "exclude_kb", "severity_floor"],
        "outputs": {"count": "int"},
    },
    "windows_update_install": {
        "label": "Install Windows updates",
        "risk": "standard",
        "required": [],
        "optional": ["classifications", "include_kb", "exclude_kb", "severity_floor"],
        "outputs": {"installed_count": "int", "failed_count": "int", "reboot_required": "bool"},
    },
    # ── System ──────────────────────────────────────────────────────────────
    "clear_temp_files": {
        "label": "Clear /tmp",
        "risk": "low",
        "required": [],
        "optional": ["older_than_days"],
        "outputs": {"removed": "int", "skipped": "int"},
    },
    "execute_script": {
        "label": "Execute script (allowlisted file or hash-approved body)",
        "risk": "high",
        "required": [],
        "optional": ["script_name", "shell", "script"],
        "outputs": {"exit_code": "int"},
    },
    "reboot": {
        "label": "Reboot host",
        "risk": "high",
        "required": [],
        "optional": ["delay_seconds", "notify", "notify_message",
                     "defer_limit", "defer_minutes"],
        "outputs": {"delay_seconds": "int", "deferral_active": "bool"},
    },
    "run_command": {
        "label": "Run shell command",
        "risk": "high",
        "required": ["command"],
        "optional": ["timeout"],
        "outputs": {"exit_code": "int"},
    },
    "set_hostname": {
        "label": "Set hostname",
        "risk": "standard",
        "required": ["hostname"],
        "optional": [],
        "outputs": {"hostname": "str"},
    },
    # ── Networking ──────────────────────────────────────────────────────────
    "add_firewall_rule": {
        "label": "Add firewall rule",
        "risk": "high",
        "required": ["port", "protocol"],
        "optional": ["action", "source", "interface"],
        "outputs": {"port": "str", "protocol": "str", "action": "str"},
    },
    "remove_firewall_rule": {
        "label": "Remove firewall rule",
        "risk": "high",
        "required": ["port", "protocol"],
        # `rule_id`: the rule's unique identifier (Windows Name/InstanceID),
        # required in practice on Windows -- netsh has no action filter, so
        # WindowsBackend.remove_rule refuses to remove by port/protocol
        # alone (it would delete every inbound rule sharing that port), and
        # deliberately does not accept `name` (DisplayName) as a substitute
        # identifier either, since DisplayName is not guaranteed unique.
        # `name` is carried for display/back-compat only. ufw and
        # firewall-cmd ignore both.
        "optional": ["action", "source", "name", "rule_id"],
        "outputs": {"port": "str", "protocol": "str", "action": "str"},
    },
    # Read-only, so low risk: it changes nothing and the Firewall tab needs it
    # on every view. The write actions above stay high.
    "list_firewall_rules": {
        "label": "List firewall rules",
        "risk": "low",
        "required": [],
        "optional": [],
        "outputs": {"supported": "bool", "enabled": "bool", "rule_count": "int"},
    },
    "set_firewall_policy": {
        "label": "Set firewall default policy",
        "risk": "high",
        "required": ["direction", "policy"],
        "optional": [],
        "outputs": {"direction": "str", "policy": "str"},
    },
    "enable_firewall": {
        "label": "Enable the firewall",
        "risk": "high",
        "required": [],
        "optional": [],
        "outputs": {"enabled": "bool"},
    },
    "disable_firewall": {
        "label": "Disable the firewall",
        "risk": "high",
        "required": [],
        "optional": [],
        "outputs": {"enabled": "bool"},
    },
    # ── User management ────────────────────────────────────────────────────
    "create_user": {
        "label": "Create user",
        "risk": "high",
        "required": ["username"],
        "optional": ["groups", "shell"],  # groups: comma-separated
        "outputs": {"username": "str"},
    },
    "delete_user": {
        "label": "Delete user",
        "risk": "high",
        "required": ["username"],
        "optional": ["remove_home"],
        "outputs": {"username": "str"},
    },
    "add_user_to_group": {
        "label": "Add user to group",
        "risk": "standard",
        "required": ["username", "group"],
        "optional": [],
        "outputs": {"username": "str", "group": "str"},
    },
    # ── Cron ────────────────────────────────────────────────────────────────
    "create_cron_job": {
        "label": "Create cron job",
        "risk": "standard",
        "required": ["schedule", "command"],
        "optional": ["user"],
        "outputs": {"user": "str"},
    },
    "delete_cron_job": {
        "label": "Delete cron job",
        "risk": "standard",
        "required": ["pattern"],
        "optional": ["user"],
        "outputs": {"user": "str", "removed": "int"},
    },
    # ── Self-management ─────────────────────────────────────────────────────
    "update_agent": {
        "label": "Update Vigil agent",
        "risk": "standard",
        "required": [],
        "optional": ["platform"],
        "outputs": {"version": "str"},
    },
    # ── Reprovisioning (docs/reprovisioning.md) ────────────────────────────
    #
    # The three destructive actions are NOT reachable via full_control or the
    # allowlist: the agent gates them on allow_reprovision alone (§4.1). Keep
    # this block in lockstep with vigil_agent.config.REPROVISION_ACTIONS —
    # apps/reprovision/test_registry.py asserts the two agree.
    "reprovision_preflight": {
        "label": "Check rebuild readiness",
        "risk": "low",
        "required": [],
        "optional": ["disk_target", "os_family"],
        "outputs": {},  # a reprovision step's only useful fact is its status; the job record carries the rest
    },
    "reprovision_stage": {
        "label": "Stage OS installer",
        "risk": "high",
        "required": ["job_id", "kernel_url", "initrd_url",
                     "kernel_sha256", "initrd_sha256"],
        "optional": [],
        "outputs": {},  # a reprovision step's only useful fact is its status; the job record carries the rest
    },
    "reprovision_commit": {
        "label": "Boot into OS installer (WIPES DISK)",
        "risk": "high",
        "required": ["job_id", "cmdline"],
        "optional": [],
        "outputs": {},  # a reprovision step's only useful fact is its status; the job record carries the rest
    },
    # High in the registry even though it only deletes staged files: the
    # agent gates it on allow_reprovision, and the risk tier drives the
    # server-side confirmation UI. Keeping the tier aligned with the gate
    # avoids a task the server thinks is casual but the agent treats as armed.
    "reprovision_cleanup": {
        "label": "Remove staged installer files",
        "risk": "high",
        "required": ["job_id"],
        "optional": [],
        "outputs": {},  # a reprovision step's only useful fact is its status; the job record carries the rest
    },
    # ── Vulnerability scanning ─────────────────────────────────────────────
    # ── Host tagging ────────────────────────────────────────────────────────
    #
    # Tags live on the server, not the host, so these are marker actions in
    # the same shape as request_nessus_scan: the agent reports the step, and
    # the server applies the change on completion.
    #
    # The tags come from the task the SERVER stored and signed, never from
    # what the agent sends back. A compromised agent can therefore only claim
    # success on a tag an operator already authorized in the definition — it
    # cannot choose the tag. That is why this can be low risk while the
    # ``agent:`` namespace stays reserved for tags an agent asserts about
    # itself at check-in.
    #
    # Sending them through the agent rather than applying them at dispatch is
    # what makes them conditional: a ``when`` predicate is evaluated on the
    # host, so "tag it role:docker if Docker is actually installed" works.
    "add_tag": {
        "label": "Add host tag",
        "risk": "low",
        "required": ["tags"],
        "optional": [],
        "outputs": {"tags": "str"},
    },
    "remove_tag": {
        "label": "Remove host tag",
        "risk": "low",
        "required": ["tags"],
        "optional": [],
        "outputs": {"tags": "str"},
    },
    # The agent emits a "please scan me" marker; the server picks it up
    # on task completion and creates a VulnScan(requested). The actual
    # scan is launched by the central Nessus instance, not the agent.
    "request_nessus_scan": {
        "label": "Request Nessus vulnerability scan",
        "risk": "low",
        "required": [],
        "optional": [],
        "outputs": {"requested": "bool"},
    },
    # Engine-agnostic alias — uses whichever network scanner the server
    # picks based on operator preference (Nessus or Greenbone). When
    # both are configured the server consults Host.preferred_scanners
    # (and falls back to Nessus). v1 server-side wiring still creates a
    # NESSUS VulnScan; the dispatcher routing lands with PR #5.
    "request_network_scan": {
        "label": "Request network vulnerability scan",
        "risk": "low",
        "required": [],
        "optional": ["engine"],  # "nessus" | "greenbone" | "" (server picks)
        "outputs": {"engine": "str"},
    },
    # Trivy is agent-local — the agent runs `trivy fs --format json …`
    # and ships the JSON back in the task output. The server's task-
    # completion handler hands that JSON to TrivyScanner.ingest_report,
    # which writes VulnFinding rows and triggers a score recompute.
    "run_trivy_scan": {
        "label": "Run Trivy vulnerability scan",
        "risk": "low",
        "required": [],
        "optional": ["scope"],  # "fs" (default — scan root filesystem) | "rootfs" | "image:<name>"
        "outputs": {"vulnerabilities": "int"},
    },
    "trivy_db_update": {
        "label": "Update Trivy vulnerability database",
        "risk": "low",
        "required": [],
        "optional": [],
        "outputs": {"updated": "bool"},
    },
    # ── Hunts (read-only) ────────────────────────────────────────────────────
    #
    # Hunt actions discover: they walk a scope and report what they find,
    # changing nothing on the host. The walk runs in a low-priority thread
    # with hard result/timeout caps, so even a full-disk hunt cannot
    # starve the host or the task.
    "hunt_file": {
        "label": "Hunt: files on disk",
        "risk": "low",
        "required": [],
        "optional": ["name", "paths", "scope", "sha256", "min_size", "max_size",
                     "modified_within_days", "older_than_days", "hash",
                     "version_lt", "version_lte", "version_gt", "version_gte",
                     "version_eq", "max_results", "timeout", "stays_open"],
        "outputs": {"matched": "bool", "count": "int", "truncated": "bool"},
    },
    "hunt_package": {
        "label": "Hunt: installed packages",
        "risk": "low",
        "required": ["name"],
        "optional": ["version_lt", "version_lte", "version_gt", "version_gte", "version_eq",
                     "manager", "max_results", "timeout", "stays_open"],
        "outputs": {"matched": "bool", "count": "int", "truncated": "bool"},
    },
    "hunt_process": {
        "label": "Hunt: running processes",
        "risk": "low",
        "required": [],
        "optional": ["name", "cmdline", "user", "max_results", "timeout", "stays_open"],
        "outputs": {"matched": "bool", "count": "int", "truncated": "bool"},
    },
    "hunt_port": {
        "label": "Hunt: listening ports",
        "risk": "low",
        "required": [],
        "optional": ["port", "protocol", "process", "max_results", "timeout", "stays_open"],
        "outputs": {"matched": "bool", "count": "int", "truncated": "bool"},
    },
    "hunt_service": {
        "label": "Hunt: services",
        "risk": "low",
        "required": ["name"],
        "optional": ["state", "start_mode", "max_results", "timeout", "stays_open"],
        "outputs": {"matched": "bool", "count": "int", "truncated": "bool"},
    },
    "hunt_registry": {
        "label": "Hunt: registry keys and values",
        "risk": "low",
        "required": ["key"],
        "optional": ["value", "data", "view", "max_results", "timeout",
                     "stays_open"],
        "outputs": {"matched": "bool", "count": "int", "truncated": "bool"},
    },
    "hunt_content": {
        "label": "Hunt: text inside files",
        "risk": "standard",
        "required": ["pattern"],
        "optional": ["name", "paths", "scope", "max_file_size", "return",
                     "max_results", "timeout", "stays_open"],
        "outputs": {"matched": "bool", "count": "int", "truncated": "bool"},
    },
}


def action_outputs(action_type: str) -> dict[str, str]:
    """Declared outputs of an action; every step also has an implicit `status`."""
    return dict(ACTION_REGISTRY.get(action_type, {}).get("outputs", {}))
