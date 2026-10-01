"""M10 must be findable by an admin and by the AI assistant; the wiki's
detection-task example is parsed straight out of the page."""
import html
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT
from apps.tasks.spec import parse_and_validate


def _wiki() -> str:
    return (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")


class DetectionDocsTests(SimpleTestCase):
    def test_section_nav_and_example(self):
        text = _wiki()
        self.assertIn('<a href="#detection-tasks" class="nav-link">', text)
        start = text.index('<section class="section" id="detection-tasks">')
        section = text[start:text.index("</section>", start)]
        for word in ("severity", "cves", "boost", "not applicable", "Deploy detection task",
                     "Organization", "Community", "Vendor", "Detection tasks only"):
            self.assertIn(word, section)
        [example] = re.findall(r"<pre>(.*?)</pre>", section, re.S)
        spec = parse_and_validate(html.unescape(example))
        self.assertEqual((spec["severity"], spec["cves"]), ("critical", ["CVE-2021-44228"]))
        self.assertEqual(spec["boost"][0]["probe"]["type"], "hunt_process")
        for anchor in re.findall(r'href="#([a-z-]+)"', section):
            self.assertIn(f'id="{anchor}"', text, anchor)

    def test_anvil_and_ai_rules(self):
        text = _wiki()
        self.assertIn('id="vuln-anvil"', text)
        self.assertIn("/api/v1/vulns/anvil/", text)
        self.assertIn("A detection task is a normal task with severity", SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.rstrip().endswith("- Never propose update_agent."))
