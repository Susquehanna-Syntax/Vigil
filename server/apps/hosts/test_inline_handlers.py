"""No inline event handlers, and every declarative one resolves.

`script-src` cannot allow inline handlers selectively: keeping any of them
means keeping 'unsafe-inline', which is the directive that would otherwise stop
an injected <script> from running at all. So the ~110 onclick/onchange
attributes became data-act/data-change names a dispatcher looks up.

That trade has one failure mode worth guarding: a typo in an attribute name is
a button that silently does nothing, which is worse than the inline version it
replaced. These tests are the guard — they run without a browser.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "server" / "templates"
JS = REPO / "server" / "static" / "js"

#: Inline handler attributes. CSP treats all of them the same.
INLINE = re.compile(r'\bon(click|change|input|submit|error|focus|blur)\s*=\s*"')

#: Handlers the dispatcher will look up on window.
DECLARED = re.compile(r'\bdata-(act|change|input|submit)="([A-Za-z_$][\w$]*)"')


def _strip_comments(src: str) -> str:
    """JS with comments blanked out, line count preserved.

    The comments in these files explain the very patterns being banned — the
    dispatcher's docstring shows an `onclick=` example, and says it uses no
    `new Function`. Scanning raw text flags the explanation as the offence.
    """
    out = []
    i, n = 0, len(src)
    in_line = in_block = in_str = False
    quote = ""
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            else:
                out.append(" ")
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                out.append("  "); i += 2; continue
            out.append("\n" if ch == "\n" else " ")
        elif in_str:
            out.append(ch)
            if ch == "\\":
                out.append(nxt); i += 2; continue
            if ch == quote:
                in_str = False
        elif ch == "/" and nxt == "/":
            in_line = True
            out.append("  "); i += 2; continue
        elif ch == "/" and nxt == "*":
            in_block = True
            out.append("  "); i += 2; continue
        elif ch in "\"'`":
            in_str = True; quote = ch
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _js_sources() -> str:
    return "\n".join(p.read_text() for p in sorted(JS.glob("vigil-*.js")))


def _template_sources():
    for path in sorted(TEMPLATES.rglob("*.html")):
        yield path, path.read_text()


class NoInlineHandlerTests(SimpleTestCase):

    def test_no_template_carries_an_inline_event_handler(self):
        offenders = []
        for path, body in _template_sources():
            for line_no, line in enumerate(body.splitlines(), 1):
                if INLINE.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{line_no}: {line.strip()[:90]}")
        self.assertEqual(
            offenders, [],
            "Inline handlers keep 'unsafe-inline' in the CSP. Use "
            'data-act="fnName" (see delegateClick / the dispatcher in '
            "vigil-utils.js):\n" + "\n".join(offenders))

    def test_no_generated_markup_carries_an_inline_event_handler(self):
        offenders = []
        for path in sorted(JS.glob("vigil-*.js")):
            code = _strip_comments(path.read_text())
            for line_no, line in enumerate(code.splitlines(), 1):
                if INLINE.search(line):
                    offenders.append(f"{path.name}:{line_no}: {line.strip()[:90]}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class DeclarativeHandlersResolveTests(SimpleTestCase):

    def test_every_named_handler_is_defined_somewhere(self):
        """A typo here is a button that does nothing, with no error until a
        user clicks it."""
        js = _js_sources()
        defined = set(re.findall(r'^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)',
                                 js, re.M))
        defined |= set(re.findall(r'^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(',
                                  js, re.M))
        defined |= set(re.findall(r'window\.([A-Za-z_$][\w$]*)\s*=', js))

        missing = []
        for path, body in _template_sources():
            for _attr, name in DECLARED.findall(body):
                if name not in defined:
                    missing.append(f"{path.relative_to(REPO)}: {name}")
        self.assertEqual(
            sorted(set(missing)), [],
            "These handlers are named in a template but defined in no "
            "vigil-*.js file:\n" + "\n".join(sorted(set(missing))))

    def test_the_dispatcher_itself_is_present(self):
        """Everything above is inert without it."""
        utils = (JS / "vigil-utils.js").read_text()
        for needed in ("data-act", "_runAct", "delegateClick"):
            self.assertIn(needed, utils)

    def test_the_dispatcher_uses_no_eval(self):
        """CSP blocks eval and new Function, and reaching for either here would
        have swapped one hole for a worse one."""
        code = _strip_comments((JS / "vigil-utils.js").read_text())
        self.assertNotIn("eval(", code, "vigil-utils.js calls eval")
        self.assertNotIn("new Function", code, "vigil-utils.js calls new Function")
