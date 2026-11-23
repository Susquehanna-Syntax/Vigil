"""Translation readiness (S6 M6 item 7): a ratchet on untranslated template text.

Only English ships, but a template on TRANSLATED_TEMPLATES has every string a
person reads inside `{% translate %}` or `{% blocktranslate %}`, so a later
language is a .po file rather than a code change. The check is deliberately
blunt: strip code, comments, translate blocks, template tags and markup, and
whatever letters are left were missed. The visible attributes (placeholder,
title, aria-label, alt, data-tip, data-hint) are held to the same rule.

Add a template to the list once it is wrapped; never take one off."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)

TRANSLATED_TEMPLATES: list[str] = [
    "templates/login.html",
    "templates/login_verify.html",
    "templates/setup.html",
    "templates/_host_card.html",
    "templates/pages/_alerts.html",
    "templates/pages/_tasks.html",
    "templates/pages/_task_editor.html",
    "templates/pages/_inventory.html",
    "templates/pages/_topology.html",
    "templates/pages/_store.html",
    "templates/pages/_dashboard.html",
    "templates/pages/_search.html",
    "templates/pages/_statuspages.html",
    "templates/pages/_playbooks.html",
    "templates/pages/_policies.html",
    "templates/pages/_monitor.html",
    "templates/pages/_reprovision.html",
    "templates/dashboard.html",
    "templates/compliance_report.html",
    "templates/pages/_firewall.html",
    "templates/pages/_hunt_results.html",
    "templates/pages/_community.html",
    "templates/pages/_reports.html",
    "templates/pages/_about.html",
    "templates/pages/_containers.html",
    "templates/pages/_apps.html",
    "templates/pages/_vulns.html",
    "templates/pages/_stack_editor.html",
    "templates/status_public.html",
    "templates/ui_kit.html",
    "templates/base.html",
    "templates/pages/_settings.html",
]

_DROP = [
    re.compile(r"<script\b.*?</script>", re.S | re.I),
    re.compile(r"<style\b.*?</style>", re.S | re.I),
    re.compile(r"<svg\b.*?</svg>", re.S | re.I),
    re.compile(r"<!--.*?-->", re.S),
    re.compile(r"\{#.*?#\}", re.S),
    re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.S),
    re.compile(r"\{%\s*blocktranslate\b.*?\{%\s*endblocktranslate\s*%\}", re.S),
    re.compile(r"\{%.*?%\}", re.S),
    re.compile(r"\{\{.*?\}\}", re.S),
]
_VISIBLE_ATTR = re.compile(
    r'\s(?:placeholder|title|aria-label|alt|data-tip|data-hint)='
    r'(?:"((?:\{%.*?%\}|\{\{.*?\}\}|[^"])*)"|\'((?:\{%.*?%\}|\{\{.*?\}\}|[^\'])*)\')', re.I | re.S)
_TAG = re.compile(r"<[^>]*>", re.S)
_ENTITY = re.compile(r"&[a-zA-Z]+;|&#\d+;")
_LETTERS = re.compile(r"[A-Za-z]{2,}")


def untranslated(text: str) -> list[str]:
    """The words a person would read that no translate tag covers."""
    # Code, comments and SVG first: a title= inside a script's string is not
    # an attribute anyone reads in this template.
    for pattern in _DROP[:6]:
        text = pattern.sub(" ", text)
    attrs = [m.group(1) if m.group(1) is not None else m.group(2) for m in _VISIBLE_ATTR.finditer(text)]
    body = text
    for pattern in _DROP:
        body = pattern.sub(" ", body)
    body = _ENTITY.sub(" ", _TAG.sub(" ", body))
    found = [" ".join(chunk.split()) for chunk in body.split("\n") if _LETTERS.search(chunk)]
    for value in attrs:
        for pattern in _DROP[-2:]:
            value = pattern.sub(" ", value)
        if _LETTERS.search(value):
            found.append(f"[attribute] {value.strip()}")
    return found


class TranslationReadyTests(SimpleTestCase):
    def test_the_checker_catches_bare_text_and_attributes(self):
        self.assertEqual(untranslated('<p>{% translate "Hosts" %}</p>'), [])
        self.assertEqual(untranslated("<p>{% blocktranslate %}Hi {{ n }}{% endblocktranslate %}</p>"), [])
        self.assertEqual(untranslated("<p>Hosts</p>"), ["Hosts"])
        self.assertEqual(untranslated('<input placeholder="Search">'), ["[attribute] Search"])
        self.assertEqual(untranslated('<input placeholder="{% translate "Search" %}">'), [])
        self.assertEqual(untranslated("<input placeholder='Search'>"), ["[attribute] Search"])
        self.assertEqual(untranslated("<input placeholder='{% translate \"Search\" %}'>"), [])
        self.assertEqual(untranslated("<script>const a = 'text';</script><p>{{ host.name }}</p>"), [])
        self.assertEqual(untranslated("<script>x = '<span title=\"' + label + '\">';</script>"), [])

    def test_translated_templates_have_no_bare_text(self):
        for rel in TRANSLATED_TEMPLATES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertRegex(text, r"\{%\s*load\s[^%]*\bi18n\b", f"{rel} must load i18n")
            self.assertEqual(untranslated(text), [], f"{rel} has text outside translate tags")
