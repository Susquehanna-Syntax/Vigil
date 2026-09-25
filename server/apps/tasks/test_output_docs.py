"""Authors can see what a step hands to the next one without reading code.

Every declared output is documented (the wiki render refuses otherwise), the
wiki lists each action's outputs, the editor preview shows them per step, and
the AI prompt knows the steps.<id>.result.<field> form.
"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT
from apps.tasks.management.commands.render_wiki_actions import OUTPUT_NOTES
from apps.tasks.spec import ACTION_REGISTRY, action_outputs, parse_and_validate

REPO = Path(settings.BASE_DIR).parent


class OutputDocsTests(SimpleTestCase):
    def test_every_declared_output_has_a_note(self):
        missing = [f"{a}.{f}" for a in ACTION_REGISTRY for f in action_outputs(a)
                   if f not in OUTPUT_NOTES.get(a, {})]
        self.assertEqual(missing, [])

    def test_wiki_lists_outputs(self):
        wiki = (REPO / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")
        self.assertTrue("<th>Output</th>" in wiki, "outputs table missing")
        self.assertTrue("steps.&lt;id&gt;.result" in wiki, "step-ref hint missing")

    def test_parsed_actions_list_their_outputs(self):
        spec = parse_and_validate(
            "name: t\nrisk: low\nactions:\n"
            "  - id: a\n    type: check_service\n    params:\n      service_name: nginx\n"
            "  - id: b\n    type: restart_service\n    params:\n      service_name: nginx\n")
        self.assertEqual(spec["actions"][0]["outputs"], ["active", "state"])
        self.assertEqual(spec["actions"][1]["outputs"], ["active"])

    def test_editor_shows_outputs_escaped(self):
        src = (Path(settings.BASE_DIR) / "static" / "js" / "vigil-tasks.js").read_text(encoding="utf-8")
        self.assertTrue("preview-step-outputs" in src, "editor outputs line missing")
        self.assertTrue("escHtml(o)" in src, "outputs must be escaped")

    def test_ai_prompt_teaches_step_refs(self):
        self.assertTrue("steps.<id>.result.<field>" in SYSTEM_PROMPT, "AI prompt must teach step refs")


#: What the agent returns for each action (agent/vigil_agent/executor.py), by field
#: name. The registry must declare exactly these, or the validator accepts a
#: reference the agent never fills, or rejects one it does.
EXPECTED = {
    "check_service": {"active", "state"},
    "hunt_file": {"matched", "count", "truncated"},
    "hunt_package": {"matched", "count", "truncated"},
    "hunt_process": {"matched", "count", "truncated"},
    "hunt_port": {"matched", "count", "truncated"},
    "hunt_service": {"matched", "count", "truncated"},
    "update_container": {"updated", "old_image_id", "new_image_id"},
    "check_docker_updates": {"checked", "outdated"},
    "run_command": {"exit_code"},
    "execute_script": {"exit_code"},
    "restart_service": {"active"}, "start_service": {"active"},
    "stop_service": {"active"}, "reload_service": {"active"},
    "enable_service": {"enabled"}, "disable_service": {"enabled"},
    "restart_container": {"running"}, "start_container": {"running"},
    "stop_container": {"running"},
    "pull_image": {"image_id"}, "remove_container": {"removed"},
    "recreate_container": {"updated", "old_image_id", "new_image_id"},
    "docker_compose_up": {"compose_file"}, "docker_compose_down": {"compose_file"},
    "clear_docker_logs": {"truncated"},
    "write_file": {"path", "bytes"}, "create_directory": {"path"},
    "delete_path": {"path", "recursive"}, "copy_file": {"src", "dest"},
    "move_file": {"src", "dest"}, "set_permissions": {"path"},
    "install_package": {"package", "manager", "installed_version"},
    "update_package": {"package", "manager", "installed_version"},
    "remove_package": {"package", "manager"},
    "run_package_updates": {"manager", "security_only"},
    "clear_temp_files": {"removed", "skipped"}, "reboot": {"delay_seconds", "deferral_active"},
    "set_hostname": {"hostname"},
    "add_firewall_rule": {"port", "protocol", "action"},
    "remove_firewall_rule": {"port", "protocol", "action"},
    "list_firewall_rules": {"supported", "enabled", "rule_count"},
    "set_firewall_policy": {"direction", "policy"},
    "enable_firewall": {"enabled"}, "disable_firewall": {"enabled"},
    "create_user": {"username"}, "delete_user": {"username"},
    "add_user_to_group": {"username", "group"},
    "create_cron_job": {"user"}, "delete_cron_job": {"user", "removed"},
}


class RegistryMatchesAgentTests(SimpleTestCase):
    def test_every_action_declares_outputs(self):
        missing = [name for name in ACTION_REGISTRY if "outputs" not in ACTION_REGISTRY[name]]
        self.assertEqual(missing, [])

    def test_registry_matches_agent_outputs(self):
        for action, fields in EXPECTED.items():
            self.assertEqual(set(action_outputs(action)), fields, action)
