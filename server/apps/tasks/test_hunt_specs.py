"""Server-side validation of hunt_file specs (M5 phase 02)."""
from django.test import SimpleTestCase, TestCase

from .spec import SpecError, parse_and_validate


def _yaml(action_params: str) -> str:
    return f"""
name: Hunt for a known-bad jar
risk: low
actions:
  - id: hunt
    type: hunt_file
    params:
{action_params}
"""


class HuntFileSpecTests(TestCase):
    def test_hunt_file_needs_name_or_sha256(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_yaml("      paths: /opt\n"))
        self.assertIn("name", str(ctx.exception))
        self.assertIn("sha256", str(ctx.exception))

    def test_hunt_file_with_name_validates(self):
        spec = parse_and_validate(_yaml('      name: "log4j-core-2.1*.jar"\n'))
        action = spec["actions"][0]
        self.assertEqual(action["type"], "hunt_file")
        self.assertEqual(action["risk"], "low")
        self.assertEqual(action["outputs"],
                         ["count", "matched", "truncated"])

    def test_hunt_file_with_sha256_only_validates(self):
        spec = parse_and_validate(_yaml('      sha256: "9999999999999999999999999999999999999999999999999999999999999999"\n'))
        self.assertEqual(spec["actions"][0]["params"]["sha256"],
                         "9" * 64)

    def test_hunt_file_unknown_params_refused(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_yaml('      name: "a.jar"\n      bogus: 1\n'))
        self.assertIn("bogus", str(ctx.exception))


class HuntPackageSpecTests(SimpleTestCase):
    def test_hunt_package_requires_name(self):
        yaml_src = ("name: t\nrisk: low\nactions:\n  - id: p\n    type: hunt_package\n"
                    "    params:\n      version_lt: \"3.0.13\"\n")
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(yaml_src)
        self.assertTrue("name" in str(ctx.exception))
