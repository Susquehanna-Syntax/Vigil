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
    def test_monitor_buttons_open_the_seeded_tasks(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        mon = (root / "static/js/vigil-monitor.js").read_text(encoding="utf-8")
        dep = (root / "static/js/vigil-deploy.js").read_text(encoding="utf-8")
        for name in ("Restart container", "Stop container", "Start container",
                     "Restart stack", "Update stack"):
            self.assertIn(f'"{name}"', mon)
        self.assertIn("/stacks/", mon)
        self.assertIn("async function openBuiltinTask(name, hostId, inputs)", dep)
