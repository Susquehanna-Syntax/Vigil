"""No template may open a ``{# #}`` comment it does not close on that line.

``{# ... #}`` comments only to end of line. Spread one across three lines and
Django renders lines two and three as page text — visible prose in the middle
of the UI, with no error anywhere to say so. This has now shipped twice, most
recently as an explanation of the host selector appearing above the host
selector.

``{% comment %}`` is the multi-line form and has no such failure mode. This
test is the reason nobody has to remember that.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase


def _template_files() -> list[Path]:
    roots = [Path(d) for engine in settings.TEMPLATES
             for d in engine.get("DIRS", [])]
    roots.append(Path(settings.BASE_DIR) / "templates")
    seen: dict[Path, None] = {}
    for root in roots:
        if root.is_dir():
            for path in root.rglob("*.html"):
                seen[path.resolve()] = None
    return sorted(seen)


def _unclosed_comments(text: str) -> list[tuple[int, str]]:
    """(line number, opening line) for every ``{#`` not closed on its line."""
    offenders = []
    for match in re.finditer(r"\{#", text):
        rest = text[match.start():]
        close, newline = rest.find("#}"), rest.find("\n")
        if close == -1 or (newline != -1 and newline < close):
            line_no = text[:match.start()].count("\n") + 1
            offenders.append((line_no, rest.splitlines()[0].strip()))
    return offenders


class TemplateCommentTests(TestCase):
    def test_no_template_has_a_multiline_hash_comment(self):
        files = _template_files()
        self.assertTrue(files, "found no templates to check — wrong root?")

        problems = [f"{path}:{line}: {text}"
                    for path in files
                    for line, text in _unclosed_comments(
                        path.read_text(encoding="utf-8"))]
        self.assertEqual(
            problems, [],
            "These {# #} comments run past their line, so every line after "
            "the first renders as visible page text. Use {% comment %} ... "
            "{% endcomment %} instead:\n" + "\n".join(problems))

    def test_the_detector_catches_a_known_bad_comment(self):
        """Guard the guard: a test that cannot fail protects nothing."""
        self.assertEqual(
            _unclosed_comments("<p>ok</p>\n{# starts here\n   leaks #}\n"),
            [(2, "{# starts here")])

    def test_a_single_line_comment_is_accepted(self):
        self.assertEqual(_unclosed_comments("{# fine #}\n<p>x</p>\n"), [])
