from django.test import TestCase

from apps.tasks.models import TaskDefinition
from apps.tasks.spec import SpecError, resolve_inputs


class UpdateContainerTaskSeedTests(TestCase):
    def setUp(self):
        self.defn = TaskDefinition.objects.get(name="Update container", owner=None)

    def test_the_update_container_task_is_seeded(self):
        self.assertEqual(self.defn.risk_level, "standard")
        self.assertEqual(
            self.defn.parsed_spec["actions"][0]["type"],
            "update_container",
        )

    def test_the_update_container_task_fills_the_container_name(self):
        resolved = resolve_inputs(self.defn.parsed_spec, {"container_name": "web"})
        self.assertEqual(
            resolved["actions"][0]["params"],
            {"container_name": "web"},
        )

    def test_the_update_container_task_requires_a_container_name(self):
        with self.assertRaises(SpecError):
            resolve_inputs(self.defn.parsed_spec, {})
