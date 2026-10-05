"""`$${{` is an escaped literal `${{`; inserted values are never rescanned.

The server writes a `${{` that appears inside an input value as `$${{`. The
runtime turns it back into `${{` in the same single pass that expands real
markers, so neither a value nor an earlier step's result can become a template.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.runtime import resolve_value


class EscapedMarkerTests(unittest.TestCase):
    CTX = {"inputs": {"pick": "alpha"}, "steps": {"s": {"status": "ok", "result": {"v": "$${{ inputs.pick }}"}}}}

    def test_escaped_marker_becomes_literal(self):
        self.assertEqual(resolve_value("echo $${{ inputs.pick }}", self.CTX), "echo ${{ inputs.pick }}")

    def test_real_marker_still_expands_next_to_an_escaped_one(self):
        self.assertEqual(resolve_value("$${{ inputs.pick }} ${{ inputs.pick }}", self.CTX),
                         "${{ inputs.pick }} alpha")

    def test_inserted_values_are_not_rescanned(self):
        # A step result that itself contains an escaped marker is inserted verbatim.
        self.assertEqual(resolve_value("got ${{ steps.s.result.v }}", self.CTX), "got $${{ inputs.pick }}")


if __name__ == "__main__":
    unittest.main()
