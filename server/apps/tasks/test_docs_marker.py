"""Every place that teaches task YAML must use the M3 marker syntax.

The old ``{{ inputs.x }}`` marker and inline ``{type, params}`` steps are gone
as of M3: the marker is ``${{ inputs.x }}`` and scripts receive inputs as
``VIGIL_INPUT_<NAME>`` environment variables. These tests pin the AI prompt,
the README example and the wiki to the new syntax so the old marker cannot
creep back into the docs.

The prompt deliberately shows the forbidden old marker in its "never write"
warning, so the prompt test excludes that one literal occurrence; the README
and wiki tests are strict.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT


def _repo_root() -> Path:
    return Path(settings.BASE_DIR).parent


# The one place a bare ``{{ inputs.x }}`` is allowed to appear: the prompt's
# own "never write this" warning.
_PROMPT_FORBIDDEN_LITERALS = 1


class NewMarkerDocsTests(SimpleTestCase):

    def test_ai_prompt_teaches_the_new_marker(self):
        self.assertIn("${{ inputs.", SYSTEM_PROMPT)
        self.assertIn("VIGIL_INPUT_", SYSTEM_PROMPT)
        bare = re.findall(r"(?<!\$)\{\{ inputs\.", SYSTEM_PROMPT)
        self.assertLessEqual(
            len(bare), _PROMPT_FORBIDDEN_LITERALS,
            f"SYSTEM_PROMPT contains {len(bare)} bare {{ inputs.x }} markers; "
            "only the single 'never write' warning is allowed")

    def test_readme_and_wiki_use_the_new_marker(self):
        for name in ("README.md", "wiki/vigil-wiki.html"):
            text = (_repo_root() / name).read_text(encoding="utf-8")
            self.assertEqual(
                re.findall(r"(?<!\$)\{\{ inputs\.", text), [],
                f"{name} still contains a bare {{ inputs.x }} marker")
