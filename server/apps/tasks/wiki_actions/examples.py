"""The example builder for the Built-in Actions page.

``apps/tasks/management/commands/render_wiki_actions.py`` reads these to write
that page: ``EXAMPLE_TITLES`` is the sample task's name and one-line blurb per
action, ``example_yaml`` assembles a complete, valid definition for an action.
"""

from __future__ import annotations

from apps.tasks.spec import ACTION_REGISTRY

from .params import EXAMPLE_INPUTS, PARAM_EXAMPLES

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
    "set_sleep_timeout": ("Stop lab laptops sleeping on battery",
                          ("Minutes before an idle machine sleeps; 0 means "
                           "never. Gives batteries some credit and desktops a "
                           "blank afternoon.")),
    "sleep_now": ("Sleep the test bench when the run finishes",
                  ("Suspends after a short delay so the result reaches Vigil "
                   "before the machine goes down.")),
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
    "raise_alert": ("Page when the disk sweep fails",
                    "Opens an alert on this host and notifies your channels. "
                    "A repeat run under the same key does not page twice."),
    "resolve_alert": ("Clear the disk-sweep alert once it succeeds",
                      "Resolves this host's open alert with that key, and "
                      "notifies the resolution."),
    "send_notification": ("Tell Ops the sweep finished",
                          "One message to every enabled channel, or to the "
                          "one you name. Creates no alert."),
    "jackil_ticket": ("Open a ticket when the backup fails",
                      "Opens a ticket for this host and key, or notes the "
                      "open one when it happens again. Does nothing while "
                      "Jackil is turned off."),
    "jackil_note": ("Say what you tried on the ticket",
                    "Adds an internal note to this host's open ticket for "
                    "the key. Does nothing when none is open."),
    "jackil_resolve": ("Close the ticket once the backup works",
                       "Optionally notes the recovery, sets the ticket's "
                       "status, and closes the link so the next failure "
                       "opens a fresh ticket."),
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


def example_yaml(action: str) -> str:
    """A complete, valid task definition using this action.

    Complete matters: a fragment showing only the params cannot be pasted into
    the editor, and a reader who pastes one gets a validation error for the
    missing name and actions list rather than a working task. Every example
    this function returns is run through ``parse_and_validate`` by the tests.
    """
    entry = ACTION_REGISTRY[action]
    title, description = EXAMPLE_TITLES.get(action, (entry["label"], ""))
    if action in ("raise_alert", "send_notification"):
        # The marker only says anything useful when the message names what an
        # earlier step found, and a step reference must name a step that
        # precedes it — so the example carries that earlier step.
        lines = [
            f"name: {title}",
            f"description: {description}",
            "risk: low",
            "actions:",
            "  - id: check-cron",
            "    type: check_service",
            "    params:",
            "      service_name: cron",
            f"  - id: {action.replace('_', '-')}",
            f"    type: {action}",
            "    params:",
        ]
        if action == "raise_alert":
            lines += ["      severity: warning", "      key: disk-cleanup"]
        lines.append('      message: "Nightly disk sweep failed. Cron state: '
                     '${{ steps.check-cron.result.state }}"')
        return "\n".join(lines) + "\n"
    if action == "resolve_alert":
        return "\n".join([
            f"name: {title}",
            f"description: {description}",
            "risk: low",
            "actions:",
            "  - id: resolve-alert",
            "    type: resolve_alert",
            "    params:",
            "      key: disk-cleanup",
        ]) + "\n"
    params = list(entry["required"])
    if not params and action == "execute_script":
        # `execute_script` accepts exactly one of script_name or an inline
        # body; the example shows the allowlisted-file form.
        params = ["script_name"]
    if action in ("app_install", "app_uninstall"):
        # `app` is the inventory id the Apps page shows, not the task's name,
        # so the example is written out rather than filled from PARAM_EXAMPLES.
        install = action == "app_install"
        return "\n".join([
            f"name: {'Install openssl' if install else 'Uninstall openssl'}",
            "description: \"Act on one app by the id the Apps page shows, on the hosts you dispatch this to.\"",
            "risk: standard",
            "actions:",
            f"  - id: {action.replace('_', '-')}",
            f"    type: {action}",
            "    params:",
            "      app: openssl",
        ]) + "\n"
    if action == "app_pin":
        # The hold is the interesting half; releasing it is the same step with
        # `unpin: true` and no version.
        return "\n".join([
            "name: Hold openssl at its version",
            "description: \"Install a given openssl and hold it, so an upgrade "
            "leaves it alone. Put `unpin: true` in place of `version` to "
            "release the hold.\"",
            "risk: standard",
            "actions:",
            "  - id: app-pin",
            "    type: app_pin",
            "    params:",
            "      app: openssl",
            '      version: "3.0.13-1"',
        ]) + "\n"
    if action == "stack_deploy":
        # Written by Vigil when you press Deploy on a stack; shown so the
        # shape is known. The .env never appears here — only a ticket id.
        return "\n".join([
            "name: Deploy media",
            "description: \"What Vigil sends when you deploy the media stack.\"",
            "risk: high",
            "actions:",
            "  - id: deploy",
            "    type: stack_deploy",
            "    params:",
            "      project: media",
            "      working_dir: /opt/vigil/stacks/media",
            "      env_ticket: 6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
            "      compose: |",
            "        services:",
            "          jellyfin:",
            "            image: jellyfin/jellyfin:10.9",
            "            env_file: .env",
        ]) + "\n"
    if action == "stack_read":
        return "\n".join([
            "name: Adopt shop",
            "description: \"What Vigil sends when you adopt the shop stack.\"",
            "risk: standard",
            "actions:",
            "  - id: read",
            "    type: stack_read",
            "    params:",
            "      project: shop",
            "      adopt_ticket: 6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
        ]) + "\n"
    if action == "stack_remove":
        return "\n".join([
            "name: Take down media",
            "description: \"Stop and remove the media stack's containers, keep its files.\"",
            "risk: standard",
            "actions:",
            "  - id: remove",
            "    type: stack_remove",
            "    params:",
            "      project: media",
            "      working_dir: /opt/vigil/stacks/media",
        ]) + "\n"
    if action == "app_ensure":
        # One rule of a policy: the state, not the command.
        return "\n".join([
            "name: Keep Firefox current",
            "description: \"Install Firefox where it is missing and upgrade it where "
            "it is behind. A second run on the same host changes nothing.\"",
            "risk: standard",
            "actions:",
            "  - id: app-1",
            "    type: app_ensure",
            "    params:",
            "      app: Mozilla.Firefox",
            "      source: winget",
            "      state: latest",
        ]) + "\n"
    if action == "app_install_custom":
        # A URL and the digest the download must match — the one action that
        # runs an installer no package manager vouches for.
        return "\n".join([
            "name: Install the Acme agent from its vendor URL",
            "description: \"Download the vendor's MSI, check it against the SHA-256 "
            "you pinned, and install it silently. Nothing runs if the hash differs.\"",
            "risk: high",
            "actions:",
            "  - id: app-install-custom",
            "    type: app_install_custom",
            "    params:",
            "      url: https://downloads.example.com/acme-agent-4.2.0.msi",
            "      sha256: 3a7f1b0c5d8e2f4a6b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c",
        ]) + "\n"
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
