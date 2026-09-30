"""Server-side validation and registration of ``app_pin``.

``app_pin`` shares the ``app`` / ``source`` / ``version`` grammar of the other
``app_*`` actions and adds one param of its own, ``unpin``, which is the only
thing here that is not already covered by ``test_app_actions_spec.py``.
"""

from django.test import SimpleTestCase

from apps.tasks.registry import ACTION_REGISTRY
from apps.tasks.spec import SpecError, parse_and_validate


def _definition(action, params):
    return (f"name: t\ndescription: d\nrisk: standard\nactions:\n"
            f"  - id: a\n    type: {action}\n    params: {params}\n")


class AppPinSpecTests(SimpleTestCase):
    def test_pin_parses_and_validates(self):
        for params in ("{app: openssl}",
                       "{app: openssl, source: dpkg}",
                       "{app: openssl, version: 3.0.13-1}",
                       "{app: Firefox, source: winget, version: 140.0.3485.81}",
                       "{app: openssl, unpin: true}",
                       "{app: openssl, source: dpkg, unpin: false}"):
            with self.subTest(params=params):
                parsed = parse_and_validate(_definition("app_pin", params))
                self.assertEqual(parsed["risk"], "standard")
                self.assertEqual(parsed["actions"][0]["type"], "app_pin")

        self.assertEqual(ACTION_REGISTRY["app_pin"]["risk"], "standard")
        self.assertEqual(ACTION_REGISTRY["app_pin"]["required"], ["app"])
        self.assertEqual(ACTION_REGISTRY["app_pin"]["optional"],
                         ["source", "version", "unpin"])
        self.assertEqual(ACTION_REGISTRY["app_pin"]["outputs"],
                         {"pinned_version": "str", "pinned": "bool"})

    def test_unpin_must_be_a_boolean(self):
        # "yes" is YAML 1.1 for true, which is what an operator writes when
        # they mean a boolean; anything else that is not a bool is refused.
        for unpin in ('"yes"', '"true"', '"1"'):
            with self.subTest(unpin=unpin), self.assertRaises(SpecError) as ctx:
                parse_and_validate(_definition(
                    "app_pin", f"{{app: openssl, unpin: {unpin}}}"))
            self.assertIn("unpin", str(ctx.exception))

        # A value resolved per deploy cannot be judged here; the agent re-checks.
        unresolved = ("name: t\ndescription: d\nrisk: standard\ninputs:\n"
                      "  - id: freeze\n    type: boolean\n    default: false\n"
                      "actions:\n  - id: a\n    type: app_pin\n"
                      "    params: {app: openssl, unpin: '${{ inputs.freeze }}'}\n")
        parse_and_validate(unresolved)

    def test_unpin_with_a_version_is_refused(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_definition(
                "app_pin", "{app: openssl, unpin: true, version: 3.0.13-1}"))
        self.assertIn("unpin takes no version", str(ctx.exception))

    def test_app_and_version_and_source_rules_are_the_shared_ones(self):
        bad = [
            "{app: 'foo;rm -rf /'}",
            "{app: '-oProxy=x'}",
            "{app: openssl, source: apt}",
            "{app: openssl, version: '3.0 13'}",
            "{app: openssl, source: snap, version: '1.2'}",
            "{app: openssl, source: flatpak, version: '1.2'}",
            "{app: openssl, source: registry, version: '1.2'}",
        ]
        for params in bad:
            with self.subTest(params=params), self.assertRaises(SpecError):
                parse_and_validate(_definition("app_pin", params))

        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_definition("app_pin", "{source: dpkg}"))
        self.assertIn("app", str(ctx.exception))

    def test_pinned_is_referenceable_by_a_later_step(self):
        spec = """name: Pin then confirm
risk: standard
actions:
  - id: pin
    type: app_pin
    params: {app: openssl, source: dpkg, version: 3.0.13-1}
  - id: free
    type: app_pin
    params: {app: openssl}
    when: steps.pin.result.pinned and steps.pin.result.pinned_version != ""
"""
        try:
            parse_and_validate(spec)
        except SpecError as exc:
            self.fail(f"a step cannot reference app_pin's outputs: {exc}")
