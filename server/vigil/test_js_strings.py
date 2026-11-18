"""Translation readiness for strings scripts build (S6 M6 item 7, part 2).

A ratchet like test_inline_styles: every vigil-*.js file's count of
user-facing strings that are not wrapped in t() / tn() (vigil-utils.js) is
pinned in js_strings_baseline.json and may only fall, and a script on
JS_TRANSLATED must have none left.

What counts as a user-facing string is a deliberately simple reading of the
places text reaches a person: text between tags in built markup, the visible
attributes (title, placeholder, aria-label, alt, data-tip, data-hint), and the
literal first argument of showToast / confirmModal or a literal assigned to
textContent / innerText, and a literal given to a display key in an object
(title, label, meta, message, hint, placeholder and the like: a modal's or a
picker's options). Comments are ignored, and so is any line carrying the
marker `i18n-ignore` (for text that is not a sentence: a shell command, a code
sample). Wrapped text reads `${escHtml(t('Rename'))}` in markup and
`showToast(t('Tag created'))` elsewhere, which none of the patterns match."""

import json
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
BASELINE_PATH = Path(__file__).with_name("js_strings_baseline.json")
SCRIPT_GLOB = "static/js/vigil-*.js"

#: Scripts with no bare user-facing string left. Add one when it reaches zero.
JS_TRANSLATED: list[str] = [
    "static/js/vigil-tags.js",
    "static/js/vigil-waves.js",
    "static/js/vigil-monitor.js",
    "static/js/vigil-inventory.js",
    "static/js/vigil-siteswitch.js",
    "static/js/vigil-runhistory.js",
    "static/js/vigil-hunts.js",
    "static/js/vigil-enroll.js",
]

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?m)(?:^|(?<=[\s;{}(),]))//.*$")
_MARKUP = re.compile(
    r"(?:<[a-zA-Z][^<>]*?|</[a-zA-Z0-9]+\s*)>"
    r"([^<>`$'\"\\]*?[A-Za-z]{2,}[^<>`$'\"\\]*?)(?=<|\$\{|`|'|\")")
_ATTR = re.compile(
    r"\b(?:title|placeholder|aria-label|data-tip|data-hint|alt)=\\?\""
    r"([^\"$\\]*[A-Za-z]{2,}[^\"$\\]*)\\?\"")
_PROPERTY = re.compile(
    r"\b(?:title|label|meta|message|hint|placeholder|empty|emptyText|subtitle|desc|description"
    r"|confirmText|confirmLabel|okLabel|cancelLabel|tooltip)\s*:\s*(['\"`])"
    r"((?:(?!\1).)*?[A-Za-z]{2,}(?:(?!\1).)*?)\1")
_CALL = re.compile(
    r"\b(?:showToast|confirmModal|textContent\s*=|innerText\s*=)\s*\(?\s*(['\"`])"
    r"((?:(?!\1).)*?[A-Za-z]{2,}(?:(?!\1).)*?)\1")


def bare_strings(js: str) -> list[str]:
    """The user-facing strings in a script that no t() or tn() wraps."""
    js = "\n".join(line for line in js.split("\n") if "i18n-ignore" not in line)
    js = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(" ", js))
    found = [m.group(1).strip() for m in _MARKUP.finditer(js) if m.group(1).strip()]
    found += [f"[attribute] {m.group(1)}" for m in _ATTR.finditer(js)]
    found += [f"[call] {m.group(2)}" for m in _CALL.finditer(js)]
    found += [f"[property] {m.group(2)}" for m in _PROPERTY.finditer(js)]
    return found


def scanned_scripts() -> list[Path]:
    return sorted(ROOT.glob(SCRIPT_GLOB))


def load_baseline() -> dict[str, int]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


class JsStringRatchetTests(SimpleTestCase):
    def test_the_checker_reads_the_sinks_and_skips_wrapped_text(self):
        self.assertEqual(bare_strings("x = `<button>Rename</button>`;"), ["Rename"])
        self.assertEqual(bare_strings("x = `<button>${escHtml(t('Rename'))}</button>`;"), [])
        self.assertEqual(bare_strings('x = `<i title="Close"></i>`;'), ["[attribute] Close"])
        self.assertEqual(bare_strings("x = `<i title=\"${escAttr(t('Close'))}\"></i>`;"), [])
        self.assertEqual(bare_strings("showToast('Tag created');"), ["[call] Tag created"])
        self.assertEqual(bare_strings("showToast(t('Tag created'));"), [])
        self.assertEqual(bare_strings("el.textContent = 'Saving';"), ["[call] Saving"])
        self.assertEqual(bare_strings("if (a > b && c < d) go();"), [])
        self.assertEqual(bare_strings("pick({ title: 'Pick a tag' });"), ["[property] Pick a tag"])
        self.assertEqual(bare_strings("pick({ title: t('Pick a tag'), key: 'name' });"), [])
        self.assertEqual(bare_strings("// <b>Old</b> note\nx = 1;"), [])
        self.assertEqual(bare_strings("cmd.textContent = `ssh ${dest}`; // i18n-ignore"), [])

    def test_no_script_gains_a_bare_string(self):
        baseline = load_baseline()
        for path in scanned_scripts():
            rel = str(path.relative_to(ROOT))
            counted = len(bare_strings(path.read_text(encoding="utf-8")))
            self.assertLessEqual(
                counted, baseline.get(rel, 0),
                f"{rel} has {counted} untranslated strings, baseline {baseline.get(rel, 0)}: "
                "wrap new text in t() / tn(), or lower the baseline when you remove some")

    def test_the_baseline_lists_no_cleared_script(self):
        baseline = load_baseline()
        for path in scanned_scripts():
            rel = str(path.relative_to(ROOT))
            if rel in baseline:
                counted = len(bare_strings(path.read_text(encoding="utf-8")))
                self.assertGreater(counted, 0, f"{rel} reached 0: remove it from the baseline")
                self.assertEqual(counted, baseline[rel],
                                 f"{rel} has {counted} now, baseline says {baseline[rel]}: lower it")

    def test_translated_scripts_have_no_bare_string(self):
        for rel in JS_TRANSLATED:
            self.assertEqual(bare_strings((ROOT / rel).read_text(encoding="utf-8")), [], rel)
