"""Server-side validation and registration of ``app_ensure``."""

from django.test import SimpleTestCase

from apps.tasks.registry import ACTION_REGISTRY
from apps.tasks.spec import SpecError, parse_and_validate


def _definition(params):
    return ("name: t\ndescription: d\nrisk: standard\nactions:\n"
            f"  - id: a\n    type: app_ensure\n    params: {params}\n")


class AppEnsureSpecTests(SimpleTestCase):
    def test_valid_rules_parse(self):
        for params in ("{app: curl, state: present}",
                       "{app: curl, state: latest, source: dpkg}",
                       "{app: openssl, state: pinned, version: 3.0.13-1}",
                       "{app: Mozilla.Firefox, state: absent, source: winget}"):
            with self.subTest(params=params):
                parsed = parse_and_validate(_definition(params))
                self.assertEqual(parsed["actions"][0]["type"], "app_ensure")

    def test_registry_entry(self):
        entry = ACTION_REGISTRY["app_ensure"]
        self.assertEqual(entry["risk"], "standard")
        self.assertEqual(entry["required"], ["app", "state"])
        self.assertEqual(entry["optional"], ["source", "version"])
        self.assertEqual(set(entry["outputs"]),
                         {"changed", "action", "version_before", "version_after"})

    def test_refusals(self):
        for params in ("{app: curl}",
                       "{app: curl, state: sideways}",
                       "{app: curl, state: pinned}",
                       "{app: curl, state: latest, version: '1.0'}",
                       "{app: lxd, state: pinned, source: snap, version: '5.0'}",
                       "{app: -oProxy=x, state: present}",
                       "{app: curl, state: present, source: brew2}"):
            with self.subTest(params=params):
                with self.assertRaises(SpecError):
                    parse_and_validate(_definition(params))
