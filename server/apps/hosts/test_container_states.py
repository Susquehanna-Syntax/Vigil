"""Stopped and crashed containers and stacks stay visible, with their exit code.

User, 2026-10-09: "make sure stack that crashed or stop are visible as stopped
or exited code 0". Docker's status text carries the code ("Exited (137) …");
the page showed only "exited", so a clean stop and a crash looked the same,
and a managed stack with no containers left vanished from the list.
"""

import re
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/vigil-containers.js"


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
        i += 1


class ContainerStateTests(SimpleTestCase):
    def setUp(self):
        self.src = JS.read_text()

    def test_rows_and_stacks_use_the_exit_aware_badges(self):
        self.assertIn("${_ctrStateBadge(c)}", self.src)
        self.assertIn("_stackStateBadge(rows)", self.src)

    def test_a_down_managed_stack_stays_listed(self):
        self.assertRegex(self.src, r"downStacks = \(stacks \|\| \[\]\)\.filter\(s => s\.ownership !== 'external'")
        self.assertIn("for (const s of downStacks)", self.src)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_badges_tell_a_clean_stop_from_a_crash(self):
        """Run the real functions from the file, not a copy of them."""
        code = "\n".join([
            "function escHtml(s){return String(s);} function escAttr(s){return String(s);}",
            re.search(r"const CTR_STATES = \[[^\]]*\];", self.src).group(0),
            _function(self.src, "_ctrExitCode"),
            _function(self.src, "_ctrStateBadge"),
            _function(self.src, "_stackStateBadge"),
            "const C=(state,status)=>({state,status});",
            "console.log(JSON.stringify([",
            "  _ctrStateBadge(C('exited','Exited (0) 2 hours ago')),",
            "  _ctrStateBadge(C('exited','Exited (137) 5 minutes ago')),",
            "  _ctrStateBadge(C('running','Up 3 hours')),",
            "  _stackStateBadge([C('exited','Exited (0) 1 hour ago')]),",
            "  _stackStateBadge([C('running','Up'), C('exited','Exited (1) now')]),",
            "  _stackStateBadge([C('dead','Dead')]),",
            "]));",
        ])
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True)
        clean, crash, running, stack_stopped, stack_partial, stack_dead = __import__("json").loads(out.stdout)
        self.assertIn("stopped (exit 0)", clean)
        self.assertIn('class="ctr-state stopped"', clean)
        self.assertIn("crashed (exit 137)", crash)
        self.assertIn('class="ctr-state exited"', crash)
        self.assertIn(">running<", running)
        self.assertIn("stopped (exit 0)", stack_stopped)
        self.assertIn("1/2 running · 1 crashed (exit 1)", stack_partial)
        self.assertIn("crashed", stack_dead)
