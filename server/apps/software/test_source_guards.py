"""apps/software/guards.py: every guard validates as a task ``when:`` and is
the one the agent's own test evaluates (agent/tests/test_source_guards.py)."""
from pathlib import Path

import yaml
from django.test import SimpleTestCase

from apps.tasks.spec import _APP_SOURCE_CHOICES, parse_and_validate

from .guards import _GUARDS, source_guard

AGENT_TEST = Path(__file__).resolve().parents[3] / "agent/tests/test_source_guards.py"


class SourceGuardTests(SimpleTestCase):
    def test_every_rule_source_has_a_guard_that_validates(self):
        self.assertEqual(set(_GUARDS), set(_APP_SOURCE_CHOICES))
        doc = {"name": "guards", "actions": [
            {"id": f"s{i}", "type": "app_ensure", "when": guard,
             "params": {"app": "x", "state": "present", "source": src}}
            for i, (src, guard) in enumerate(_GUARDS.items())]}
        self.assertEqual(len(parse_and_validate(yaml.safe_dump(doc))["actions"]), len(_GUARDS))
        self.assertEqual(source_guard(""), "")

    def test_the_agent_evaluates_the_same_guards(self):
        agent_test = AGENT_TEST.read_text(encoding="utf-8")
        for src, guard in _GUARDS.items():
            self.assertIn(f'"{src}": {guard!r}', agent_test)
