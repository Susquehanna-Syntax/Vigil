"""M11 must be findable by an admin and by the AI assistant."""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.aisuggest.views import SYSTEM_PROMPT


class ContainerDocsTests(SimpleTestCase):
    def test_wiki(self):
        text = (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")
        start = text.index('<section class="section" id="docker">')
        section = text[start:text.index("</section>", start)]
        for anchor in ("containers-engines", "containers-lifecycle", "containers-managed-stacks",
                       "containers-registries", "containers-podman"):
            self.assertIn(f'id="{anchor}"', section)
        self.assertNotIn("usermod", section, "never tell anyone to join the docker group")
        for word in ("vigil-engine-proxy", "one-time ticket", "vigil-rollback.override.yaml",
                     "config --hash", "X-Registry-Auth", "/opt/vigil/stacks"):
            self.assertIn(word, section)
        for anchor in re.findall(r'href="#([a-z-]+)"', section):
            self.assertIn(f'id="{anchor}"', text, anchor)

    def test_ai_rules(self):
        self.assertIn("never run docker or podman via a script", SYSTEM_PROMPT)
        self.assertIn("Never write stack_deploy or stack_read yourself", SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.rstrip().endswith("- Never propose update_agent."))
