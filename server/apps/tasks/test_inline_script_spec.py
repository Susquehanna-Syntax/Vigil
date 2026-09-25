"""The server validates inline script bodies and records their hash.

An inline ``script`` body is arbitrary code, so the server must never
rewrite it (its hash is identical on every deploy) and must refuse
``${{ inputs.x }}`` markers inside it: inputs reach the body as
``$VIGIL_INPUT_<ID>`` environment variables.
"""

from importlib import util
from pathlib import Path

from django.test import SimpleTestCase

from apps.tasks import scripthash
from apps.tasks.spec import SpecError, parse_and_validate, resolve_inputs

BODY = r'''find /opt/app -name "*.tmp" -exec rm {} \;
echo "Removed temp files for $VIGIL_INPUT_APP"
'''


def _spec(body: str, extra_params: str = "", script_name: str | None = None) -> str:
    lines = []
    if script_name is None:
        lines.append("      shell: bash")
    if script_name is not None:
        lines.append(f"      script_name: {script_name}")
    else:
        lines.append("      script: |")
        lines.extend(f"        {l}" for l in body.rstrip("\n").split("\n"))
    lines.extend(extra_params.split("\n") if extra_params else [])
    params = "\n".join(lines)
    return f"""name: cleanup
description: Remove temp files.
risk: high
inputs:
  - id: app
    label: App
    type: text
actions:
  - id: cleanup
    type: execute_script
    params:
{params}
"""


class InlineScriptSpecTests(SimpleTestCase):
    def test_inline_body_parses_and_records_its_hash(self):
        parsed = parse_and_validate(_spec(BODY))
        action = parsed["actions"][0]
        self.assertEqual(action["script_sha256"], scripthash.script_hash(BODY))
        self.assertTrue(action["script_sha256"].startswith("sha256:"))

    def test_body_is_never_substituted(self):
        body = "echo '{{ inputs.app }} and {{.Names}}'\n"
        parsed = parse_and_validate(_spec(body))
        resolved = resolve_inputs(parsed, {"app": "x"})
        self.assertEqual(resolved["actions"][0]["params"]["script"], body)

    def test_hash_is_stable_across_deploys(self):
        parsed = parse_and_validate(_spec(BODY))
        first = resolve_inputs(parsed, {"app": "a"})
        second = resolve_inputs(parsed, {"app": "b"})
        self.assertEqual(first["actions"][0]["params"]["script"],
                         second["actions"][0]["params"]["script"])
        self.assertEqual(
            scripthash.script_hash(first["actions"][0]["params"]["script"]),
            parsed["actions"][0]["script_sha256"],
        )

    def test_markers_in_body_are_refused(self):
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(_spec("echo '${{ inputs.app }}'\n"))
        self.assertIn("VIGIL_INPUT_", str(ctx.exception))

    def test_name_and_body_together_are_refused(self):
        spec = """name: cleanup
description: Both name and body.
risk: high
inputs:
  - id: app
    label: App
    type: text
actions:
  - id: cleanup
    type: execute_script
    params:
      script_name: cleanup.sh
      shell: bash
      script: |
        echo hi
"""
        with self.assertRaises(SpecError) as ctx:
            parse_and_validate(spec)
        self.assertIn("not both", str(ctx.exception))

    def test_body_without_shell_is_refused(self):
        spec = """name: cleanup
description: No shell.
risk: high
actions:
  - id: cleanup
    type: execute_script
    params:
      script: |
        echo hi
"""
        with self.assertRaises(SpecError):
            parse_and_validate(spec)

    def test_script_name_still_works(self):
        parsed = parse_and_validate(_spec("", script_name="cleanup.sh"))
        action = parsed["actions"][0]
        self.assertEqual(action["params"]["script_name"], "cleanup.sh")
        self.assertNotIn("script_sha256", action)

    def test_server_and_agent_hash_agree(self):
        agent_copy = (
            Path(__file__).resolve().parents[3]
            / "agent" / "vigil_agent" / "scripthash.py"
        )
        if not agent_copy.exists():
            self.skipTest("agent copy not present in this checkout")
        spec = util.spec_from_file_location("agent_scripthash", agent_copy)
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for body in ("echo hi\n", "echo hi\r\n", "echo hi"):
            self.assertEqual(module.script_hash(body),
                             scripthash.script_hash(body),
                             f"hash mismatch for {body!r}")
