"""The task editor's starter templates and the preview's syntax warnings.

The editor's starter templates are what an author copies from when writing a
new task, so they must teach the new ${{ inputs.x }} syntax. The markers live
inside JS template literals (backtick strings), where an unescaped ${ starts a
JS interpolation and breaks the whole file — every marker must be written with
a backslash before the ${ so the string the browser sees is ${{ inputs.x }}.

Vigil has no JS test runner; these SimpleTestCases read the JS source the way
test_inline_handlers.py does.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

JS = Path(settings.BASE_DIR) / "static" / "js" / "vigil-tasks.js"


class TemplateMarkerTests(SimpleTestCase):

    def setUp(self):
        self.src = JS.read_text()

    def test_templates_use_the_new_marker(self):
        self.assertTrue("\\${{ inputs.pkg }}" in self.src, "the templates no longer show \\${{ inputs.pkg }}")
        self.assertEqual(
            re.findall(r"(?<!\\\$)\{\{ inputs\.", self.src),
            [],
            "an un-escaped {{ inputs.x }} marker still teaches the old syntax",
        )

    def test_no_unescaped_marker_in_template_literals(self):
        self.assertEqual(
            re.findall(r"(?<!\\)\$\{\{", self.src),
            [],
            "an unescaped ${{ inside the JS template literal is a syntax error",
        )


class PreviewWarningsTests(SimpleTestCase):

    def test_preview_renders_warnings_escaped(self):
        src = JS.read_text()
        self.assertTrue("spec.warnings" in src, "the preview does not read spec.warnings")
        self.assertTrue("escHtml(w)" in src, "warning text is not escaped before rendering")
