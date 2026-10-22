"""Power and sleep actions on the server side.

The registry is what a definition is validated against and what the wiki is
generated from, so the risk tiers here decide who has to approve a step that
puts a laptop to sleep. The security point is the split: five of these change a
setting an operator can undo, and the two that take the machine down are high
risk and — agent-side — refused unless the host runs in full_control.
"""
from django.test import SimpleTestCase

from .spec import ACTION_REGISTRY, SpecError, action_outputs, parse_and_validate


class PowerRegistryTests(SimpleTestCase):
    def test_the_seven_are_registered(self):
        for action in ("set_sleep_timeout", "set_display_timeout", "set_hibernate",
                       "set_power_plan", "set_lid_action", "sleep_now",
                       "hibernate_now"):
            with self.subTest(action=action):
                self.assertIn(action, ACTION_REGISTRY)

    def test_the_settings_are_standard_and_the_two_sleeps_high(self):
        for action in ("set_sleep_timeout", "set_display_timeout", "set_hibernate",
                       "set_power_plan", "set_lid_action"):
            with self.subTest(action=action):
                self.assertEqual(ACTION_REGISTRY[action]["risk"], "standard")
        for action in ("sleep_now", "hibernate_now"):
            with self.subTest(action=action):
                self.assertEqual(ACTION_REGISTRY[action]["risk"], "high")

    def test_the_ac_value_is_required_and_the_battery_one_optional(self):
        self.assertEqual(ACTION_REGISTRY["set_sleep_timeout"]["required"],
                         ["ac_minutes"])
        self.assertEqual(ACTION_REGISTRY["set_sleep_timeout"]["optional"],
                         ["dc_minutes"])
        self.assertEqual(ACTION_REGISTRY["set_lid_action"]["required"], ["ac"])
        self.assertEqual(ACTION_REGISTRY["set_lid_action"]["optional"], ["dc"])
        self.assertEqual(ACTION_REGISTRY["sleep_now"]["optional"], ["delay_seconds"])

    def test_outputs_match_what_the_agent_reports(self):
        self.assertEqual(set(action_outputs("set_sleep_timeout")),
                         {"ac_minutes", "dc_minutes"})
        self.assertEqual(set(action_outputs("set_hibernate")), {"enabled"})
        self.assertEqual(set(action_outputs("hibernate_now")), {"delay_seconds"})

    def test_labels(self):
        self.assertEqual(ACTION_REGISTRY["set_hibernate"]["label"],
                         "Turn hibernation on or off")
        self.assertEqual(ACTION_REGISTRY["sleep_now"]["label"], "Sleep now")


class PowerSpecTests(SimpleTestCase):
    YAML = ("name: power\nrisk: standard\nactions:\n"
            "  - id: sleep-timeout\n    type: set_sleep_timeout\n"
            "    params:\n      ac_minutes: 20\n")

    def test_a_definition_using_set_sleep_timeout_validates(self):
        spec = parse_and_validate(self.YAML)
        self.assertEqual(spec["actions"][0]["params"], {"ac_minutes": 20})
        self.assertEqual(spec["risk"], "standard")

    def test_an_unknown_param_is_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(self.YAML + "      foo: bar\n")
        self.assertIn("foo", str(ctx.exception))
