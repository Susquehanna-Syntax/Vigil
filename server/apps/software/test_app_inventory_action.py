"""`app_inventory` is a registered action the spec validator accepts.

The action's only job is to make the agent re-collect its software list and
ship it with the next check-in, so the server side is entirely about its
contract: no params, low risk, and four declared outputs a later step can
branch on.
"""

from django.test import SimpleTestCase

from apps.tasks.spec import SpecError, parse_and_validate

SPEC_YAML = """
name: Refresh inventory
risk: low
actions:
  - id: inv
    type: app_inventory
  - id: report
    type: hunt_file
    when: steps.inv.result.outdated > 0
    params: {name: '*.log'}
"""


class AppInventorySpecTests(SimpleTestCase):
    def test_app_inventory_needs_no_params_and_is_low_risk(self):
        parsed = parse_and_validate(SPEC_YAML)
        self.assertEqual(parsed["risk"], "low")
        self.assertEqual([a["type"] for a in parsed["actions"]],
                         ["app_inventory", "hunt_file"])

    def test_outputs_are_referenceable_by_a_later_step(self):
        try:
            parse_and_validate(SPEC_YAML)
        except SpecError as exc:
            self.fail(f"a step cannot reference app_inventory's outputs: {exc}")

    def test_unknown_output_reference_is_still_refused(self):
        # The guard the test above needs: a field app_inventory does not
        # declare must fail the same way, or "no error" proves nothing.
        source = SPEC_YAML.replace("result.outdated", "result.nonsense")
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(source)
        self.assertIn("nonsense", str(ctx.exception))
