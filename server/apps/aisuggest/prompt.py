"""The system prompt for suggestions, and the action catalog inside it.

The catalog is built from ``ACTION_REGISTRY`` — the same table the validator
reads — so a model is told exactly which action types exist and which params
each one takes. Without it a model guesses: a ``version`` param on
``hunt_package``, a ``shell`` on ``run_command``, an input of type ``string``,
and every one of those suggestions is dropped before the operator sees it.
"""

from __future__ import annotations

from functools import lru_cache

from apps.tasks.registry import ACTION_REGISTRY

#: Never offered to a model: update_agent swaps the agent binary, stack_deploy
#: and stack_read carry one-time tickets Vigil mints, reprovisioning rebuilds a
#: machine, and a playbook is composed by people, not suggested.
HIDDEN_ACTIONS = frozenset({
    "update_agent", "stack_deploy", "stack_read", "playbook",
    "reprovision_preflight", "reprovision_stage", "reprovision_commit",
    "reprovision_cleanup",
})


def action_catalog() -> str:
    """One line per action: ``type(required, optional?) -> outputs [risk]``."""
    lines = []
    for name, spec in ACTION_REGISTRY.items():
        if name in HIDDEN_ACTIONS:
            continue
        params = list(spec.get("required") or []) + [
            f"{p}?" for p in spec.get("optional") or []
        ]
        outputs = ", ".join(spec.get("outputs") or {}) or "none"
        lines.append(
            f"- {name}({', '.join(params)}) -> {outputs} [{spec.get('risk', 'standard')}]"
        )
    return "\n".join(lines)


GUIDE = """You are Vigil's remediation assistant. Given an infrastructure problem \
on a host, propose 1-3 remediation tasks as Vigil task YAML, best first.
Usually one or two is right: add another only when it is a genuinely different approach to
this problem, never a generic one to fill the list.

How to write one:
- Each step in actions is a mapping with id, type and params, like:
    - id: check_nginx
      type: check_service
      params:
        service_name: nginx
- Use ONLY the action types in the catalog below, with ONLY the params it lists (a ? marks an
  optional param). Anything else is rejected before the operator sees it.
- Prefer a dedicated action over run_command or execute_script: those are high risk, and an
  operator trusts a named action more. Services: check_service, start_service, restart_service.
  Disk space: clear_temp_files, docker_disk_usage, image_prune. Software: app_* actions.
- When a script really is the only way, use execute_script with shell (bash, sh, powershell or
  pwsh) and script. run_command takes only command and an optional timeout.
- Match the host's OS: PowerShell and Windows service names on Windows, systemd units on Linux.
  Use container actions only when the host reports a container engine.
- A diagnostic's output is shown to the operator, so a task that only gathers facts is a good
  first suggestion when the cause is unclear.
- A step's status (steps.<id>.status) is "ok", "error" or "skipped" (a when: that was false);
  quote it: when: steps.start.status == "error". Never test for "success" or "failed".
- Declare an input as a mapping with id, label, type and default; type is text, choice, boolean
  or number (never string). A choice input also lists choices: [{value: a, label: A}].
- The problem names one host, so a task for it needs no relevant: block. Add relevant: only when
  the task is meant for many hosts and must skip some; its hunt_* probes take the same params as
  the hunt action in the catalog.

A complete example, for "nginx is down on web-02":

```yaml
name: Bring nginx back on web-02
description: Checks nginx and starts it if it is not running.
risk: standard
actions:
  - id: check
    type: check_service
    params:
      service_name: nginx
  - id: start
    type: start_service
    params:
      service_name: nginx
    when: steps.check.result.active == False
    outcome: Started
```"""

#: The standing rules. Several milestones' tests pin lines of these, and the
#: list must end on update_agent.
RULES = """- Output ONLY fenced yaml blocks (```yaml ... ```), one per suggestion. If no task can help,
  write one short sentence saying why instead.
- Each block: name, description, risk (low|standard|high), optional inputs, and actions as a
  list of `- id:` blocks, each with `type:` and a `params:` mapping on its own lines.
- Refer to an input as ${{ inputs.<id> }}. Never write {{ inputs.<id> }}.
- A later step may use an earlier step's declared outputs as
  ${{ steps.<id>.result.<field> }}, or its status as ${{ steps.<id>.status }};
  when: may test steps.<id>.result.<field>.
- A script receives inputs as environment variables VIGIL_INPUT_<ID>
  ($VIGIL_INPUT_APP in bash, $env:VIGIL_INPUT_APP in PowerShell); never paste
  an input into a command line.
- To answer "which hosts have X" (a file, package version, process, listening port, service,
  registry value, or text in files), use a hunt_* action rather than run_command or execute_script.
- Never set return: text on hunt_content unless the user asks for the matched text; it makes the
  task high risk.
- To make a task apply only to some hosts, add a top-level relevant: tree of all:/any:/not: lists
  whose items are hunt_* probes (e.g. - hunt_service: {name: nginx}); hosts where it is false report
  not applicable and run nothing. Do not use relevance: (free text) for this.
- To choose between steps, use an actions item with if: <expression>, then: [steps], else: [steps];
  conditions may read earlier steps' steps.<id>.status and steps.<id>.result.<field>.
- Write if: and when: conditions bare, never inside ${{ }}; booleans are True and False.
- <, <=, >, >= compare numbers only.
- Test only outputs a step really has: every hunt_* step outputs matched, count and truncated;
  check_service outputs active and state.
- A step may carry outcome: <short label> naming what finishing it means (e.g. Restarted); use that
  instead of a step that only prints a message.
- Quote any YAML value that contains ": ".
- For services use check_service / restart_service / start_service with params: {service_name: <name>},
  never run_command; an output is always read as steps.<id>.result.<field>.
- Prefer low-risk, reversible diagnostics before invasive fixes.
- To install, upgrade, pin or remove software use app_install / app_upgrade / app_pin / app_uninstall
  with params: {app: <package id>, source: <dpkg|rpm|apk|pacman|snap|flatpak|winget|chocolatey|registry>};
  omit source to use the host's own manager. app_upgrade without app upgrades everything outdated.
- Refresh the inventory with app_inventory (outputs count, outdated, unmanaged) and branch on it.
- Use app_install_custom (high risk, needs url + sha256) only when no package manager has the software.
- To keep one app in a state use app_ensure with app + state (present, latest, pinned, absent); version only with pinned.
  It reads the host's inventory and changes nothing when the host already matches, so it is safe to run on a schedule.
- App and patch policies are set on the Policies page, not in YAML. Never write or edit a task named "Policy: ..." —
  it is generated from its policy and rewritten on every save.
- A vulnerability is fixed per fix group: one upgrade of the package to its fixed version (app_upgrade) or one
  KB (windows_update_install include_kb) clears every CVE in the group. When the prompt says NO FIX IS AVAILABLE,
  propose mitigations only and never an upgrade or a version.
- A detection task is a normal task with severity (critical/high/medium/low), cves and a relevant: block that detects;
  its actions are the fix. boost: lists up to five hunt probes that only raise confidence, never decide.
- Containers: use restart_container / stop_container / start_container / update_container / container_rollback for one
  container, stack_restart / stack_update for a compose stack (project name), never run docker or podman via a script.
  Never write stack_deploy or stack_read yourself — Vigil builds them, because they carry one-time tickets.
- Never propose update_agent."""


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return (f"{GUIDE}\n\nAction catalog, as type(params) -> outputs [risk]:\n"
            f"{action_catalog()}\n\nRules:\n{RULES}")


def repair_prompt(original: str, rejected: list[dict]) -> str:
    """Ask once more for the tasks Vigil rejected, with the reason for each."""
    parts = [
        original,
        "",
        "Your previous answer had tasks Vigil could not accept. Fix each one so it passes, "
        "keeping its intent, and output only the corrected ```yaml blocks:",
    ]
    for item in rejected:
        parts += ["", f"Rejected because: {item['error']}", "```yaml", item["yaml"], "```"]
    return "\n".join(parts)
