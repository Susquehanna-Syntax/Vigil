"""Loading, empty and failed states (S6 M6, list states).

vigil-utils.js has one helper per state: loadingHtml / showLoading, emptyHtml /
showEmpty, and showLoadFailure. A ratchet like test_js_strings pins, per
script, two things that bypass them, and the counts may only fall:

- a swallowed failure: a catch that sets a list to [] (or returns) without
  calling showLoadFailure, so a failed request is drawn as an empty list. A
  catch that is meant to be quiet (a clipboard write, an optional extra) says
  so with a `state-ok: <why>` comment inside it;
- hand-made loading text: a "Loading" literal outside loadingHtml()/showLoading().
"""

import json
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from vigil.test_js_strings import _BLOCK_COMMENT, _LINE_COMMENT

ROOT = Path(settings.BASE_DIR)
BASELINE_PATH = Path(__file__).with_name("list_states_baseline.json")

_SWALLOW = re.compile(r"catch\s*(?:\(\s*\w*\s*\))?\s*\{([^{}]*)\}")
_LOADING = re.compile(r"(?<![\w$.])(t\()?(['\"`])Loading(?:…|\.\.\.)?[^'\"`]*\2|>\s*Loading(?:…|\.\.\.)")


def swallowed(js: str) -> list[str]:
    """Catches that turn a failure into an empty list (or into nothing at all)."""
    found = []
    for m in _SWALLOW.finditer(js):
        body = m.group(1)
        if any(k in body for k in ("showLoadFailure", "showToast", "throw", "state-ok")):
            continue
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", body, flags=re.S)
        if re.search(r"=\s*\[\]|=\s*\{\}", code) or re.fullmatch(r"\s*(return;?)?\s*", code):
            found.append(f"line {js.count(chr(10), 0, m.start()) + 1}")
    return found


def hand_loading(js: str) -> list[str]:
    """Loading text not drawn by loadingHtml()/showLoading()."""
    js = re.sub(r"(?:loadingHtml|showLoading)\([^()]*(?:\([^()]*\)[^()]*)*\)", "", js)
    return [f"line {js.count(chr(10), 0, m.start()) + 1}" for m in _LOADING.finditer(js)]


def counts(path: Path) -> int:
    raw = path.read_text(encoding="utf-8")
    if path.name == "vigil-utils.js":  # the helpers themselves hold the default label
        raw = raw.replace("escHtml(label || t('Loading…'))", "")
    return len(swallowed(raw)) + len(hand_loading(_LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(" ", raw))))


class ListStateRatchetTests(SimpleTestCase):
    def test_the_checker_reads_swallows_and_hand_loading(self):
        self.assertEqual(swallowed("try { a(); } catch { rows = []; }"), ["line 1"])
        self.assertEqual(swallowed("try { a(); } catch (e) { return; }"), ["line 1"])
        self.assertEqual(swallowed("try { a(); } catch (e) { /* fine */ }"), ["line 1"])
        self.assertEqual(swallowed("try { a(); } catch (e) { /* state-ok: clipboard is optional */ }"), [])
        self.assertEqual(swallowed("try { a(); } catch (e) {\n  // why\n  showError(e.message);\n}"), [])
        self.assertEqual(swallowed("try { a(); } catch (e) { showLoadFailure(el, t('x'), e, go); }"), [])
        self.assertEqual(swallowed("try { a(); } catch (e) { showToast(e.message, 'error'); }"), [])
        self.assertEqual(hand_loading("el.innerHTML = `<div>${escHtml(t('Loading…'))}</div>`;"), ["line 1"])
        self.assertEqual(hand_loading("el.innerHTML = '<p>Loading...</p>';"), ["line 1"])
        self.assertEqual(hand_loading("el.innerHTML = loadingHtml(t('Loading hosts…'));"), [])
        self.assertEqual(hand_loading("showLoading(el);"), [])

    def test_no_script_gains_a_bypass(self):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        for path in sorted(ROOT.glob("static/js/vigil-*.js")):
            rel = str(path.relative_to(ROOT))
            n = counts(path)
            self.assertLessEqual(n, baseline.get(rel, 0),
                                 f"{rel}: {n} swallowed failures or hand-made loading text, baseline "
                                 f"{baseline.get(rel, 0)}: use showLoadFailure / loadingHtml")
            if rel in baseline:
                self.assertEqual(n, baseline[rel], f"{rel} has {n} now: lower the baseline (remove it at 0)")
                self.assertGreater(n, 0, f"{rel} reached 0: remove it from the baseline")
