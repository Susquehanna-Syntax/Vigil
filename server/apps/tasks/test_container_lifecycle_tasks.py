"""M11 06: the seeded container and stack lifecycle tasks, and stack actions."""
from django.test import TestCase

from apps.tasks.models import TaskDefinition
from apps.tasks.registry import ACTION_REGISTRY
from apps.tasks.spec import SpecError, parse_and_validate


class LifecycleTemplateTests(TestCase):
    def test_seeded_and_valid(self):
        for name, action, input_id in (("Restart container", "restart_container", "container_name"),
                                       ("Stop container", "stop_container", "container_name"),
                                       ("Start container", "start_container", "container_name"),
                                       ("Restart stack", "stack_restart", "project"),
                                       ("Update stack", "stack_update", "project")):
            with self.subTest(name=name):
                d = TaskDefinition.objects.get(name=name, owner=None, visibility="community")
                spec = parse_and_validate(d.yaml_source)
                self.assertEqual(spec["actions"][0]["type"], action)
                self.assertEqual([i["id"] for i in spec["inputs"]], [input_id])

    def test_stack_actions_registered(self):
        for action in ("stack_restart", "stack_update"):
            entry = ACTION_REGISTRY[action]
            self.assertEqual((entry["risk"], entry["required"]), ("standard", ["project"]))
        with self.assertRaises(SpecError):
            parse_and_validate("name: t\nactions:\n  - type: stack_restart\n    params: {}\n")


class LifecycleButtonsWiringTests(TestCase):
    def test_containers_page_buttons_open_the_seeded_tasks(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        mon = (root / "static/js/vigil-containers.js").read_text(encoding="utf-8")
        dep = (root / "static/js/vigil-deploy.js").read_text(encoding="utf-8")
        for name in ("Restart container", "Stop container", "Start container",
                     "Restart stack", "Update stack"):
            self.assertIn(f'"{name}"', mon)
        self.assertIn("/stacks/", mon)
        self.assertIn("async function openBuiltinTask(name, hostId, inputs)", dep)


class ComposeBuildParamTests(TestCase):
    """2026.15.1: `build` is a true/false on docker_compose_up and stack_deploy."""

    def test_compose_up_takes_build(self):
        base = "name: t\nactions:\n  - type: docker_compose_up\n    params:\n      compose_file: /srv/a/compose.yaml\n"
        for ok in ("true", "false", '"true"', '"${{ inputs.rebuild }}"'):
            with self.subTest(ok=ok):
                spec = base + f"      build: {ok}\n"
                if "inputs" in ok:
                    spec = spec.replace("actions:", "inputs:\n  - id: rebuild\n    type: boolean\nactions:")
                parse_and_validate(spec)
        with self.assertRaises(SpecError):
            parse_and_validate(base + "      build: sometimes\n")

    def test_stack_deploy_build_is_a_bool(self):
        from apps.tasks.spec import _validate_stack_params
        params = {"project": "web", "working_dir": "/opt/vigil/stacks/web",
                  "compose": "services:\n  a:\n    image: nginx\n"}
        _validate_stack_params({**params, "build": True}, 1, "stack_deploy")
        with self.assertRaises(SpecError):
            _validate_stack_params({**params, "build": "yes"}, 1, "stack_deploy")

    def test_a_git_stacks_update_button_redeploys(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        ctr = (root / "static/js/vigil-containers.js").read_text(encoding="utf-8")
        stk = (root / "static/js/vigil-stacks.js").read_text(encoding="utf-8")
        self.assertIn("managedGitStack(stack)", ctr)
        self.assertIn("updateGitStack(btn.dataset.stackGitUpdate)", ctr)
        body = stk[stk.index("async function updateGitStack"):]
        self.assertIn("/deploy/", body[:body.index("\n}\n")])

