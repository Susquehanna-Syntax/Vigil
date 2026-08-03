"""Resolved inputs must reach the agent (GitHub #17).

A step gated on `when: inputs.x == "yes"` was skipped on every run. The
server resolved and validated the input, substituted it into params — and
then never shipped it. The agent builds its predicate context from
`params["variables"]`, which nothing ever set, so `inputs.x` resolved to
None and every comparison came out false. Silently: a missing key is not an
error to the evaluator, it is just falsy.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition

User = get_user_model()

YAML = """
name: Conditional bench
risk: standard
inputs:
  - id: run_bench
    type: text
    label: Run benchmark?
    required: true
actions:
  - id: always
    type: check_service
    params: { service_name: sshd }
  - id: bench_prefill
    type: check_service
    when: 'inputs.run_bench == "yes"'
    params: { service_name: cron }
"""


class InputsReachTheAgentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dep", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="h1", agent_token="tok-h1", status=Host.Status.ONLINE,
            mode=Host.Mode.FULL_CONTROL)
        self.definition = TaskDefinition.objects.create(
            owner=self.user, name="Conditional bench", yaml_source=YAML,
            parsed_spec=self._parse())

    def _parse(self):
        from apps.tasks.spec import parse_and_validate

        return parse_and_validate(YAML)

    def _deploy(self, inputs):
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            return self.client.post(
                f"/api/v1/tasks/definitions/{self.definition.id}/deploy/",
                {"host_ids": [str(self.host.id)], "totp": "123456",
                 "inputs": inputs},
                content_type="application/json")

    def test_deploy_ships_resolved_inputs_as_variables(self):
        resp = self._deploy({"run_bench": "yes"})
        self.assertEqual(resp.status_code, 201, resp.content)
        task = Task.objects.get(host=self.host)
        self.assertEqual(task.params.get("variables"), {"run_bench": "yes"})

    def test_variables_present_even_with_no_declared_inputs(self):
        """An empty dict is still meaningful — it distinguishes 'no inputs'
        from 'inputs were dropped somewhere in the pipeline'."""
        yaml_no_inputs = """
name: Plain
risk: standard
actions:
  - id: a
    type: check_service
    params: { service_name: sshd }
"""
        from apps.tasks.spec import parse_and_validate

        d = TaskDefinition.objects.create(
            owner=self.user, name="Plain", yaml_source=yaml_no_inputs,
            parsed_spec=parse_and_validate(yaml_no_inputs))
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self.client.post(
                f"/api/v1/tasks/definitions/{d.id}/deploy/",
                {"host_ids": [str(self.host.id)], "totp": "123456"},
                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        task = Task.objects.filter(host=self.host).order_by("-created_at").first()
        self.assertEqual(task.params.get("variables"), {})

    def test_when_predicate_survives_into_the_step(self):
        self._deploy({"run_bench": "yes"})
        task = Task.objects.get(host=self.host)
        gated = [s for s in task.params["steps"] if s["id"] == "bench_prefill"]
        self.assertEqual(len(gated), 1)
        self.assertIn("inputs.run_bench", gated[0]["when"])


class PredicateEvaluationTests(TestCase):
    """End-to-end against the agent's own evaluator, with the context the
    agent actually builds from what the server now sends."""

    def _agent_context(self, params):
        import sys
        from pathlib import Path

        agent_dir = Path(__file__).resolve().parents[3] / "agent"
        sys.path.insert(0, str(agent_dir))
        try:
            from vigil_agent.expression import evaluate
        finally:
            sys.path.remove(str(agent_dir))
        # Mirrors _build_when_context's inputs source.
        return evaluate, {"agent": {"os": "linux"},
                          "inputs": params.get("variables") or {},
                          "host": {}}

    def test_gated_step_runs_when_the_input_matches(self):
        evaluate, ctx = self._agent_context({"variables": {"run_bench": "yes"}})
        self.assertTrue(evaluate('inputs.run_bench == "yes"', ctx))

    def test_gated_step_skips_when_the_input_differs(self):
        evaluate, ctx = self._agent_context({"variables": {"run_bench": "no"}})
        self.assertFalse(evaluate('inputs.run_bench == "yes"', ctx))

    def test_membership_predicate_works(self):
        evaluate, ctx = self._agent_context({"variables": {"run_bench": "Yes"}})
        self.assertTrue(
            evaluate('inputs.run_bench in ("yes", "Yes", "YES", "y")', ctx))

    def test_the_original_bug_reproduces_without_variables(self):
        """Regression marker: with no variables the predicate is silently
        false, which is what made every gated step skip."""
        evaluate, ctx = self._agent_context({})
        self.assertFalse(evaluate('inputs.run_bench == "yes"', ctx))


class UndeclaredInputInPredicateTests(TestCase):
    """A typo'd input name must fail the save, not skip forever (#17)."""

    def _parse(self, yaml_source):
        from apps.tasks.spec import parse_and_validate

        return parse_and_validate(yaml_source)

    def test_undeclared_input_in_when_is_rejected(self):
        from apps.tasks.spec import SpecError

        bad = """
name: Typo
risk: standard
inputs:
  - id: run_bench
    type: text
    label: Run?
actions:
  - id: a
    type: check_service
    when: 'inputs.run_bnech == "yes"'
    params: { service_name: sshd }
"""
        with self.assertRaises(SpecError) as ctx:
            self._parse(bad)
        self.assertIn("run_bnech", str(ctx.exception))

    def test_error_explains_the_consequence(self):
        from apps.tasks.spec import SpecError

        bad = """
name: Typo
risk: standard
actions:
  - id: a
    type: check_service
    when: 'inputs.nope == "yes"'
    params: { service_name: sshd }
"""
        with self.assertRaises(SpecError) as ctx:
            self._parse(bad)
        self.assertIn("never run", str(ctx.exception))

    def test_declared_input_in_when_is_accepted(self):
        good = """
name: Fine
risk: standard
inputs:
  - id: run_bench
    type: text
    label: Run?
actions:
  - id: a
    type: check_service
    when: 'inputs.run_bench == "yes"'
    params: { service_name: sshd }
"""
        spec = self._parse(good)
        self.assertEqual(spec["actions"][0]["when"], 'inputs.run_bench == "yes"')

    def test_agent_only_predicate_needs_no_inputs(self):
        good = """
name: Agent gate
risk: standard
actions:
  - id: a
    type: check_service
    when: 'agent.os == "linux"'
    params: { service_name: sshd }
"""
        spec = self._parse(good)
        self.assertEqual(spec["actions"][0]["when"], 'agent.os == "linux"')
