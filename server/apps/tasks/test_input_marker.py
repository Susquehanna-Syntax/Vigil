"""Input marker syntax: ${{ inputs.x }} cannot collide, the old form warns.

Braces meant too many things: YAML, bash, PowerShell, Docker templates —
so {{ inputs.x }} inside script text was ambiguous. The ${{ inputs.x }}
marker is not valid in any of those languages, so it cannot be mistaken
for script text. The pre-2026.13 {{ inputs.x }} form still resolves but
collects a save-time warning for one release.
"""
from django.test import SimpleTestCase

from apps.tasks.spec import SpecError, parse_and_validate, resolve_inputs


def _yaml(service_name: str, inputs: bool = True) -> str:
    inputs_block = (
        ("  - id: pkg\n    type: text\n    label: Package?\n") if inputs else ""
    )
    inputs_key = "inputs:\n" if inputs else ""
    return (
        "name: Marker test\n"
        "risk: standard\n"
        f"{inputs_key}"
        f"{inputs_block}"
        "actions:\n"
        "  - id: a\n"
        "    type: check_service\n"
        f"    params:\n"
        f"      service_name: {service_name}\n"
    )


class InputMarkerTests(SimpleTestCase):
    def test_new_marker_resolves(self):
        parsed = parse_and_validate(_yaml("${{ inputs.pkg }}"))
        resolved = resolve_inputs(parsed, {"pkg": "nginx"})
        self.assertEqual(resolved["actions"][0]["params"]["service_name"], "nginx")

    def test_old_marker_still_resolves_and_warns(self):
        parsed = parse_and_validate(_yaml("'{{ inputs.pkg }}'"))
        resolved = resolve_inputs(parsed, {"pkg": "nginx"})
        self.assertEqual(resolved["actions"][0]["params"]["service_name"], "nginx")
        self.assertEqual(len(parsed["warnings"]), 1)
        self.assertIn("old input syntax", parsed["warnings"][0])

    def test_new_marker_does_not_warn(self):
        parsed = parse_and_validate(_yaml("${{ inputs.pkg }}"))
        self.assertEqual(parsed["warnings"], [])

    def test_unknown_new_marker_is_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_yaml("${{ inputs.nope }}"))
        self.assertIn("unknown input reference", str(ctx.exception))

    def test_steps_marker_must_name_an_earlier_step(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_yaml("${{ steps.a.result.x }}"))
        self.assertIn("not an earlier step", str(ctx.exception))

    def test_bare_braces_are_not_inputs(self):
        yaml_src = (
            "name: Marker test\n"
            "risk: standard\n"
            "actions:\n"
            "  - id: a\n"
            "    type: run_command\n"
            "    params:\n"
            "      command: 'docker ps --format {{.Names}}'\n"
        )
        parsed = parse_and_validate(yaml_src)
        self.assertEqual(parsed["warnings"], [])
        resolved = resolve_inputs(parsed, {})
        self.assertEqual(
            resolved["actions"][0]["params"]["command"],
            "docker ps --format {{.Names}}",
        )

    def test_mixed_forms_resolve_in_one_value(self):
        parsed = parse_and_validate(_yaml("${{ inputs.pkg }}-{{ inputs.pkg }}"))
        resolved = resolve_inputs(parsed, {"pkg": "nginx"})
        self.assertEqual(
            resolved["actions"][0]["params"]["service_name"], "nginx-nginx"
        )
