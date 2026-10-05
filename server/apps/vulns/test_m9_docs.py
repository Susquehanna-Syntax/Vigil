"""M9 must be findable by an admin and by the AI assistant."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT


def _section() -> tuple[str, str]:
    text = (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")
    start = text.index('<section class="section" id="vulns">')
    return text, text[start:text.index("</section>", start)]


class VulnEvidenceDocsTests(SimpleTestCase):
    def test_wiki_covers_m9(self):
        text, section = _section()
        for anchor in ("vuln-fix-groups", "vuln-matcher", "vuln-evidence", "vuln-remediation-by-fix"):
            self.assertIn(f'id="{anchor}"', section)
        for word in ("one row per group", "OSV.dev", "CISA KEV", "FIRST EPSS", "import_vuln_data",
                     "sync_vuln_data", "match_vulns", "urgent", "file evidence", "Deploy fix",
                     "Suggest mitigations", "source</em> name"):
            self.assertIn(word, section)
        for anchor in re.findall(r'href="#([a-z-]+)"', section):
            self.assertIn(f'id="{anchor}"', text, anchor)

    def test_ai_rule(self):
        self.assertIn("A vulnerability is fixed per fix group", SYSTEM_PROMPT)
        self.assertIn("NO FIX IS AVAILABLE", SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.rstrip().endswith("- Never propose update_agent."))
