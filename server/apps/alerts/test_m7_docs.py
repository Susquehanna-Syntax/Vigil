"""The wiki's Alerts section must not drift from what M7 shipped.

The five new subsections are checked for their headings and order; the numbers
an admin reads (group size, how many hosts a grouped notice names, the Slack
ack cap, the signature skew) are checked against the constants in code, and
every registered channel type's label must be named.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

from apps.alerts import notifications, slack_actions, tasks
from apps.alerts.channels import all_types, slack


def _wiki() -> str:
    return (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text(encoding="utf-8")


def _section(text: str) -> str:
    start = text.index('<section class="section" id="alerts">')
    return text[start:text.index("<section", start + 10)]


def _subsections(section: str) -> dict[str, str]:
    """Body of each <h3> in the alerts section, keyed by its id ("" when none)."""
    heads = [(m.start(), m.group(1) or "") for m in
             re.finditer(r'<h3(?: id="([^"]*)")?>', section)]
    out: dict[str, str] = {}
    for i, (pos, hid) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(section)
        out[hid] = section[pos:end]
    return out


class M7AlertsWikiTests(SimpleTestCase):
    IDS = ("alert-channels", "maintenance-windows", "host-dependencies",
           "grouped-notices", "slack-acknowledge")

    def test_five_subsections_in_order(self):
        text = _wiki()
        section = _section(text)
        positions = []
        for hid in self.IDS:
            self.assertIn(f'<h3 id="{hid}">', section, hid)
            positions.append(text.index(f'<h3 id="{hid}">'))
        self.assertEqual(positions, sorted(positions))
        for anchor in re.findall(r'href="#([a-z-]+)"', section):
            self.assertIn(f'id="{anchor}"', text, anchor)

    def test_grouped_notice_numbers_match_code(self):
        body = _subsections(_section(_wiki()))["grouped-notices"]
        for value in (tasks.GROUP_MIN, notifications.GROUP_NAMES_SHOWN):
            self.assertRegex(body, rf"\b{value}\b", str(value))

    def test_slack_acknowledge_numbers_and_path(self):
        body = _subsections(_section(_wiki()))["slack-acknowledge"]
        self.assertRegex(body, rf"\b{slack.MAX_ACK_IDS}\b")
        self.assertIn(reverse("alert-slack-action").lstrip("/"), body)
        self.assertIn("5 minutes", body)
        self.assertEqual(slack_actions.MAX_SKEW_SECONDS, 300)

    def test_every_channel_type_label_is_named(self):
        body = _subsections(_section(_wiki()))["alert-channels"]
        for channel_type in all_types():
            self.assertIn(channel_type.label, body, channel_type.kind)


class M7AlertsWikiStyleTests(SimpleTestCase):
    def test_no_emoji_or_coloured_bar(self):
        section = _section(_wiki())
        for c in section:
            if ord(c) > 0x2600:
                self.assertIn(c, ("→", "—", "–"), hex(ord(c)))
        self.assertNotIn("border-left", section)
