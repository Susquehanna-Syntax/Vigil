"""Wide dialogs get a corner grip that resizes them, and remember the size.

User, 2026-10-15: "We should also be able to resize the modals that we're
editing in." The playbook, automation and policy editors, the reprovision
profile, the deploy dialog and the stacked task dialogs are all too small to
work in. The grip is added from JS, so no template carries it — which means
these tests read the JS and CSS source rather than a rendered page, and the
clamp (the one piece with real arithmetic) runs for real under node.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/vigil-modal.js"
CSS = ROOT / "static/css/vigil.css"


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
        i += 1


def _section(src: str) -> str:
    """The resizable-dialog section: from its selector to the end of the file."""
    return src[src.index("const _RESIZABLE_SEL"):]


def _try_bodies(text: str):
    """The body of every `try { … }` in `text`, brace-balanced."""
    bodies = []
    for m in re.finditer(r"try\s*\{", text):
        depth, i = 0, m.end() - 1
        while i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                break
            i += 1
        bodies.append(text[m.end():i])
    return bodies


class ResizableModalTests(SimpleTestCase):
    def setUp(self):
        self.src = JS.read_text()
        self.css = CSS.read_text()

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_clamp(self):
        """Run the real clamp from the file, not a copy of it."""
        code = "\n".join([
            _function(self.src, "modalClampSize"),
            "console.log(JSON.stringify([",
            "  modalClampSize(100, 100, 1920, 1080),",
            "  modalClampSize(5000, 5000, 1920, 1080),",
            "  modalClampSize(700.4, 500.6, 1920, 1080),",
            "  modalClampSize(800, 600, 300, 200),",
            "]));",
        ])
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True,
                             timeout=30, check=True)
        tiny, huge, rounded, small_window = json.loads(out.stdout)
        self.assertEqual(tiny, {"w": 360, "h": 240})
        self.assertEqual(huge, {"w": 1888, "h": 1048})
        self.assertEqual(rounded, {"w": 700, "h": 501})
        # A window smaller than the minimum: the max wins, so nothing off-screen.
        self.assertEqual(small_window, {"w": 268, "h": 168})

    def test_grip_only_on_wide_dialogs(self):
        sel = re.search(r"const _RESIZABLE_SEL = '([^']*)'", self.src).group(1)
        for variant in ("modal-wide", "modal-xwide", "modal-flow"):
            self.assertIn(variant, sel)
        self.assertIn("data-resizable", sel)
        self.assertRegex(self.src, r"_resizableInit\(el\)\s*\{\s*\n\s*"
                                   r"if \(!el\.matches\(_RESIZABLE_SEL\) \|\| "
                                   r"el\.matches\('\.m-pop'\)\) return;")

    def test_the_open_modal_hook_calls_the_init(self):
        hook = re.search(r"_modalStack\.push\(\{[^\n]*\n\s*(_resizableInit\(el\);)", self.src)
        self.assertIsNotNone(hook, "the stack push does not call _resizableInit")
        self.assertLess(self.src.index(hook.group(1)), self.src.index("_focusInto(el);"))

    def test_size_is_remembered_safely(self):
        section = _section(self.src)
        self.assertIn("vigil.modalSize.", section)
        outside = sum(body.count("localStorage") for body in _try_bodies(section))
        self.assertEqual(section.count("localStorage"), outside,
                         "a localStorage use sits outside a try block")
        self.assertGreater(outside, 0)

    def test_grip_is_keyboard_and_pointer_driven(self):
        for fragment in ("pointerdown", "setPointerCapture", "dblclick",
                         "ArrowRight", "ArrowUp", "ArrowLeft", "ArrowDown",
                         "setAttribute('aria-label', 'Resize dialog')"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.src)
        self.assertIn("ev.shiftKey ? 64 : 16", self.src)

    def test_no_side_stripe(self):
        grip = self.css[self.css.index(".modal-grip"):]
        for rule in re.findall(r"\.modal-grip[^{]*\{([^}]*)\}", grip):
            self.assertNotIn("border-left", rule)
            self.assertNotIn("border-right", rule)
        self.assertIn("border: 0;", grip)
