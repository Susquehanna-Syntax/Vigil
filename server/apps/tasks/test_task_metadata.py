"""Optional task metadata fields — cves, references, platforms.

Phase 02 of M4: purely additive. Every task that validates today must
still validate unchanged with the three new keys defaulting to [].
"""
from django.test import TestCase

from apps.tasks.spec import SpecError, parse_and_validate

MINIMAL_YAML = "name: Minimal\nactions:\n  - type: check_service\n    params: { service_name: nginx }\n"


def with_metadata(**fields: str) -> str:
    yaml_src = MINIMAL_YAML
    for key, value in fields.items():
        yaml_src += f"{key}: {value}\n"
    return yaml_src


class TaskMetadataTests(TestCase):
    def test_metadata_fields_default_to_empty(self):
        spec = parse_and_validate(MINIMAL_YAML)
        self.assertEqual(spec["cves"], [])
        self.assertEqual(spec["references"], [])
        self.assertEqual(spec["platforms"], [])

    def test_cves_are_normalized_and_uppercased(self):
        spec = parse_and_validate(with_metadata(cves='[" cve-2024-3094 ", CVE-2025-49706]'))
        self.assertEqual(spec["cves"], ["CVE-2024-3094", "CVE-2025-49706"])

    def test_cves_rejects_bare_string(self):
        with self.assertRaises(SpecError):
            parse_and_validate(with_metadata(cves="CVE-2024-3094"))

    def test_cves_rejects_malformed_id(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(with_metadata(cves='["CVE-24-1"]'))
        self.assertIn("CVE-24-1", str(ctx.exception))

    def test_cves_rejects_duplicates(self):
        with self.assertRaises(SpecError):
            parse_and_validate(with_metadata(cves='["CVE-2024-3094", "cve-2024-3094"]'))

    def test_references_rejects_non_http_schemes(self):
        for bad in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "ftp://x/y",
            "not-a-url",
            "https:///evil",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(SpecError):
                    parse_and_validate(with_metadata(references=f'["{bad}"]'))

    def test_references_accepts_http_and_https(self):
        spec = parse_and_validate(
            with_metadata(
                references='["http://example.com/advisory", "https://nvd.nist.gov/vuln/detail/CVE-2024-3094"]'
            )
        )
        self.assertEqual(
            spec["references"],
            ["http://example.com/advisory", "https://nvd.nist.gov/vuln/detail/CVE-2024-3094"],
        )

    def test_platforms_validates_membership(self):
        spec = parse_and_validate(with_metadata(platforms='["Windows", "LINUX", darwin]'))
        self.assertEqual(spec["platforms"], ["windows", "linux", "darwin"])
        for bad in ("freebsd", "macos"):
            with self.subTest(platform=bad):
                with self.assertRaises(SpecError):
                    parse_and_validate(with_metadata(platforms=f'["{bad}"]'))

    def test_existing_yaml_is_unaffected(self):
        yaml_src = (
            "name: Older fields\n"
            "description: exercises the pre-metadata schema\n"
            "relevance: web servers\n"
            "risk: high\n"
            "inputs:\n"
            "  - id: image\n"
            "    type: text\n"
            "    label: Image tag\n"
            "    required: true\n"
            "schedule:\n"
            "  window:\n"
            "    start_hour: 8\n"
            "    end_hour: 17\n"
            "target_tags:\n"
            "  - os:linux\n"
            "actions:\n"
            "  - id: bounce\n"
            "    type: restart_service\n"
            "    params: { service_name: nginx }\n"
        )
        spec = parse_and_validate(yaml_src)
        self.assertEqual(spec["name"], "Older fields")
        self.assertEqual(spec["description"], "exercises the pre-metadata schema")
        self.assertEqual(spec["relevance"], "web servers")
        self.assertEqual(spec["risk"], "high")
        self.assertEqual(spec["inputs"][0]["id"], "image")
        self.assertEqual(spec["schedule"]["window"]["start_hour"], 8)
        self.assertEqual(spec["schedule"]["window"]["end_hour"], 17)
        self.assertEqual(spec["target_tags"], ["os:linux"])
        self.assertEqual(spec["actions"][0]["type"], "restart_service")
        self.assertEqual(spec["author"], "")
        self.assertEqual(spec["created"], "")
        self.assertIsNone(spec["on_failure"])
        self.assertIsNone(spec["success_criteria"])
        self.assertIsNone(spec["collect"])
        self.assertEqual(spec["cves"], [])
        self.assertEqual(spec["references"], [])
        self.assertEqual(spec["platforms"], [])
