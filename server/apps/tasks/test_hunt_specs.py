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


class HuntProcessSpecTests(SimpleTestCase):
    def test_hunt_process_needs_a_filter(self):
        yaml_src = ("name: t\nrisk: low\nactions:\n  - id: h\n    type: hunt_process\n"
                    "    params:\n      max_results: 10\n")
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(yaml_src)
        self.assertIn("name, cmdline or user", str(ctx.exception))

    def test_hunt_process_with_name_validates(self):
        yaml_src = ("name: t\nrisk: low\nactions:\n  - id: h\n    type: hunt_process\n"
                    "    params:\n      name: \"java\"\n")
        spec = parse_and_validate(yaml_src)
        self.assertEqual(spec["actions"][0]["outputs"],
                         ["count", "matched", "truncated"])


class HuntPortSpecTests(SimpleTestCase):
    def test_hunt_port_needs_port_or_process(self):
        yaml_src = ("name: t\nrisk: low\nactions:\n  - id: h\n    type: hunt_port\n"
                    "    params:\n      protocol: tcp\n")
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(yaml_src)
        self.assertIn("port", str(ctx.exception))
        self.assertIn("process", str(ctx.exception))

    def test_hunt_port_with_port_validates(self):
        yaml_src = ("name: t\nrisk: low\nactions:\n  - id: h\n    type: hunt_port\n"
                    "    params:\n      port: 8443\n")
        spec = parse_and_validate(yaml_src)
        self.assertEqual(spec["actions"][0]["outputs"],
                         ["count", "matched", "truncated"])


class HuntServiceSpecTests(SimpleTestCase):
    def test_hunt_service_requires_name(self):
        yaml_src = ("name: t\nrisk: low\nactions:\n  - id: h\n    type: hunt_service\n"
                    "    params:\n      state: stopped\n")
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(yaml_src)
        self.assertIn("name", str(ctx.exception))


def _content_yaml(params: str) -> str:
    return f"""
name: Hunt for leaked text
risk: standard
actions:
  - id: hunt
    type: hunt_content
    params:
{params}
"""


def _registry_yaml(params: str) -> str:
    return f"""
name: Hunt the registry
risk: low
actions:
  - id: hunt
    type: hunt_registry
    params:
{params}
"""


class HuntContentSpecTests(SimpleTestCase):
    def test_content_text_makes_task_high_risk(self):
        spec = parse_and_validate(_content_yaml(
            '      pattern: "AKIA[0-9A-Z]{16}"\n      return: text\n'))
        self.assertEqual(spec["risk"], "high")

    def test_content_lines_stays_standard(self):
        spec = parse_and_validate(_content_yaml(
            '      pattern: "AKIA[0-9A-Z]{16}"\n      return: lines\n'))
        self.assertEqual(spec["risk"], "standard")
        spec = parse_and_validate(_content_yaml(
            '      pattern: "AKIA[0-9A-Z]{16}"\n'))
        self.assertEqual(spec["risk"], "standard")
        spec = parse_and_validate(_content_yaml(
            '      pattern: "AKIA[0-9A-Z]{16}"\n      return: match\n'))
        self.assertEqual(spec["risk"], "standard")

    def test_content_return_validated(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_content_yaml(
                '      pattern: "x"\n      return: everything\n'))
        self.assertIn("return", str(ctx.exception))

    def test_content_pattern_validated(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_content_yaml('      pattern: "([unclosed"\n'))
        self.assertIn("pattern", str(ctx.exception))

    def test_content_requires_pattern(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_content_yaml("      name: \"*.conf\"\n"))
        self.assertIn("pattern", str(ctx.exception))

    def test_content_unknown_params_refused(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_content_yaml(
                '      pattern: "x"\n      bogus: 1\n'))
        self.assertIn("bogus", str(ctx.exception))


class HuntRegistrySpecTests(SimpleTestCase):
    def test_registry_requires_key(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_registry_yaml('      value: DisplayName\n'))
        self.assertIn("key", str(ctx.exception))

    def test_registry_with_key_validates(self):
        spec = parse_and_validate(_registry_yaml(
            "      key: 'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion'\n"))
        action = spec["actions"][0]
        self.assertEqual(action["type"], "hunt_registry")
        self.assertEqual(action["risk"], "low")
        self.assertEqual(action["outputs"],
                         ["count", "matched", "truncated"])
