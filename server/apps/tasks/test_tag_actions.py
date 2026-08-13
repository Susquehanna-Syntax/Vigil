"""add_tag / remove_tag: task-driven changes to a host's tags.

Tags are server-side metadata about a host, not state on it, so these are
marker actions in the same shape as request_nessus_scan — the agent reports
the step and the server applies the change on completion.

The security point: the tags applied come from the task the SERVER stored
and signed, never from the agent's reported output. A compromised agent can
at most claim success on a tag an operator already wrote into the definition;
it cannot choose one. That is what keeps these low-risk while ``agent:``
stays reserved for tags an agent asserts about itself at check-in.
"""
import json

from django.test import TestCase

from apps.hosts.models import Host

from .models import Task
from .spec import ACTION_REGISTRY, SpecError, parse_and_validate
from .views import _maybe_apply_tags


class TagActionRegistryTests(TestCase):
    def test_both_actions_are_registered(self):
        for action in ("add_tag", "remove_tag"):
            self.assertIn(action, ACTION_REGISTRY)
            self.assertEqual(ACTION_REGISTRY[action]["risk"], "low")
            self.assertEqual(ACTION_REGISTRY[action]["required"], ["tags"])

    def test_the_agent_knows_both_actions(self):
        """Three places have to agree or the task fails on the host with
        'unknown action' after passing every server-side check."""
        import ast
        from pathlib import Path

        from django.conf import settings

        agent = Path(settings.BASE_DIR).parent / "agent" / "vigil_agent"
        if not agent.is_dir():
            self.skipTest("agent source not present in this build")

        allowlist = (agent / "config.py").read_text()
        executor = (agent / "executor.py").read_text()
        for action in ("add_tag", "remove_tag"):
            self.assertIn(f'"{action}"', allowlist,
                          f"{action} missing from the agent allowlist")
            self.assertIn(f'"{action}": _', executor,
                          f"{action} missing from the agent's handler table")
        ast.parse(executor)  # the handler table must still be valid Python


class TagSpecValidationTests(TestCase):
    def _yaml(self, tags, action="add_tag"):
        return (f"name: t\nrisk: low\nactions:\n"
                f"  - type: {action}\n    params:\n      tags: {tags}\n")

    def test_a_comma_separated_string_is_accepted(self):
        spec = parse_and_validate(self._yaml('"role:web, env:lab"'))
        self.assertEqual(spec["actions"][0]["params"]["tags"],
                         "role:web, env:lab")

    def test_a_list_is_refused_with_a_reason(self):
        """Params must be primitives so the signed payload stays flat."""
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(self._yaml("[role:web, env:lab]"))
        self.assertIn("comma-separated", str(ctx.exception))

    def test_the_reserved_namespace_is_refused_at_save_time(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(self._yaml('"agent:linux"'))
        self.assertIn("reserved", str(ctx.exception))

    def test_an_empty_tag_list_is_refused(self):
        with self.assertRaises(SpecError):
            parse_and_validate(self._yaml('"  ,  "'))

    def test_an_overlong_tag_is_refused(self):
        with self.assertRaises(SpecError):
            parse_and_validate(self._yaml('"' + "x" * 41 + '"'))

    def test_remove_tag_is_validated_the_same_way(self):
        with self.assertRaises(SpecError):
            parse_and_validate(self._yaml('"agent:linux"', action="remove_tag"))


class TagApplicationTests(TestCase):
    """What lands on the host when the task completes."""

    def setUp(self):
        self.host = Host.objects.create(
            hostname="h", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
            tags=["site:hq"])

    def _task(self, action, params):
        return Task.objects.create(
            host=self.host, action=action, params=params,
            state=Task.State.COMPLETED)

    def _apply(self, action, tags):
        _maybe_apply_tags(self._task(action, {"tags": tags}))
        self.host.refresh_from_db()
        return self.host.tags

    def test_a_tag_is_added(self):
        self.assertEqual(self._apply("add_tag", "role:web"),
                         ["site:hq", "role:web"])

    def test_several_tags_are_added(self):
        self.assertEqual(self._apply("add_tag", "role:web, env:lab"),
                         ["site:hq", "role:web", "env:lab"])

    def test_a_tag_is_removed(self):
        self.assertEqual(self._apply("remove_tag", "site:hq"), [])

    def test_removing_a_tag_the_host_lacks_is_a_no_op(self):
        self.assertEqual(self._apply("remove_tag", "nope"), ["site:hq"])

    def test_adding_a_tag_twice_does_not_duplicate_it(self):
        self.assertEqual(self._apply("add_tag", "site:hq"), ["site:hq"])

    def test_the_reserved_namespace_is_refused_here_too(self):
        """Re-checked at write time: this path writes straight to the host
        without going through the spec validator, and a templated value is
        only resolved by now."""
        self.assertEqual(self._apply("add_tag", "agent:linux, role:web"),
                         ["site:hq", "role:web"])

    def test_an_agent_tag_cannot_be_removed_by_a_task(self):
        self.host.tags = ["agent:linux"]
        self.host.save()
        self.assertEqual(self._apply("remove_tag", "agent:linux"),
                         ["agent:linux"])

    def test_a_multi_step_task_applies_every_tag_step(self):
        """Multi-step tasks arrive as _script with params.steps — one match
        is not enough here, unlike the scan marker."""
        task = self._task("_script", {"steps": [
            {"action": "add_tag", "params": {"tags": "role:web"}},
            {"action": "restart_service", "params": {"service_name": "nginx"}},
            {"action": "add_tag", "params": {"tags": "env:lab"}},
        ]})
        _maybe_apply_tags(task)
        self.host.refresh_from_db()
        self.assertEqual(self.host.tags, ["site:hq", "role:web", "env:lab"])

    def test_add_and_remove_in_one_task_both_apply(self):
        task = self._task("_script", {"steps": [
            {"action": "add_tag", "params": {"tags": "role:web"}},
            {"action": "remove_tag", "params": {"tags": "site:hq"}},
        ]})
        _maybe_apply_tags(task)
        self.host.refresh_from_db()
        self.assertEqual(self.host.tags, ["role:web"])

    def test_a_task_with_no_tag_steps_leaves_tags_alone(self):
        task = self._task("restart_service", {"service_name": "nginx"})
        _maybe_apply_tags(task)
        self.host.refresh_from_db()
        self.assertEqual(self.host.tags, ["site:hq"])

    def test_tagging_never_raises(self):
        """A task result must stay recordable even if tagging fails."""
        task = self._task("add_tag", {"tags": None})
        _maybe_apply_tags(task)  # must not raise
        self.host.refresh_from_db()
        self.assertEqual(self.host.tags, ["site:hq"])

    def test_the_agents_reported_output_cannot_choose_the_tag(self):
        """The whole trust argument: tags come from the stored task."""
        task = self._task("add_tag", {"tags": "role:web"})
        task.result_output = "Tag requested: root:pwned — server will apply"
        task.save()
        _maybe_apply_tags(task)
        self.host.refresh_from_db()
        self.assertEqual(self.host.tags, ["site:hq", "role:web"])
