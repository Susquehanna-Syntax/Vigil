"""M12: one modal behaviour for every dialog and drawer (SQSY design language).

Vigil has no JS test runner, so these read the sources the way
test_inline_handlers.py does; the behaviour itself — focus in, Tab trap, Esc
closes only the top dialog, inert page, focus return, stacked recede — was
checked in a browser against the design language's checklist.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

SERVER = Path(__file__).resolve().parents[2]
JS = SERVER / "static" / "js"
CSS = (SERVER / "static" / "css" / "vigil.css").read_text()


def _src(name):
    return (JS / name).read_text()


class ModalPrimitiveTests(SimpleTestCase):
    def test_loaded_straight_after_utils(self):
        base = (SERVER / "templates" / "base.html").read_text()
        utils = base.index("js/vigil-utils.js")
        modal = base.index("js/vigil-modal.js")
        nxt = base.index("<script", utils + 1)
        self.assertLess(utils, modal)
        self.assertGreater(nxt, utils)
        self.assertLess(modal, base.index("js/vigil-nav.js"))

    def test_escape_is_captured_and_closes_only_the_top(self):
        src = _src("vigil-modal.js")
        self.assertIn("window.addEventListener('keydown'", src)
        self.assertIn("e.stopImmediatePropagation()", src)
        self.assertRegex(src, r"\}, true\);\s*$|\}, true\);\n")
        # The old net closed the first open modal in DOM order, not the top.
        self.assertNotIn("document.querySelector('.modal.open')", _src("vigil-utils.js"))

    def test_class_writes_are_guarded_against_waking_the_observer(self):
        src = _src("vigil-modal.js")
        for line in src.splitlines():
            if "classList.remove('recede')" in line:
                self.assertIn("classList.contains('recede')", line, line)

    def test_focus_trap_inert_and_return(self):
        src = _src("vigil-modal.js")
        self.assertIn("sib.inert = true", src)
        self.assertIn("e.key !== 'Tab'", src)
        self.assertIn("trig.focus(", src)


class ModalStyleTests(SimpleTestCase):
    def test_closed_dialogs_are_hidden_from_tab(self):
        block = CSS[CSS.index(".modal {"):CSS.index(".modal.open {")]
        self.assertIn("visibility: hidden", block)
        panel = CSS[CSS.index(".detail-panel {"):CSS.index(".detail-panel.open")]
        self.assertIn("visibility: hidden", panel)

    def test_motion_reads_the_tokens(self):
        self.assertIn("--m-dur: 300ms;", CSS)
        self.assertIn("var(--m-ease)", CSS)
        self.assertIn("blur(var(--m-blur))", CSS)
        self.assertIn("calc(var(--m-dur) * 0.7)", CSS)

    def test_variants_exist_and_are_assigned(self):
        for rule in (".modal.m-pop", ".modal.m-sheet-sm", ".modal.open.recede",
                     ".modal-overlay.stacked", "@keyframes shake"):
            self.assertIn(rule, CSS)
        self.assertIn("mountModal('confirm', { variant: 'm-pop' })", _src("vigil-utils.js"))
        self.assertIn("variant: 'm-pop m-sheet-sm'", _src("vigil-pickers.js"))
        self.assertIn("shakeEl(hostnameInput)", _src("vigil-reprovision.js"))

    def test_no_side_stripes_in_the_modal_layer(self):
        modal_css = CSS[CSS.index("MODAL\n"):CSS.index(".form-group {")]
        self.assertIsNone(re.search(r"border-(left|right):\s*[2-9]px", modal_css))
