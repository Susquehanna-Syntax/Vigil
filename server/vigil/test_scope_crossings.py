"""Every place code crosses site scope, pinned (S1 phase 04a).

``Model.objects.unscoped(reason="...")`` and ``scoping.system(reason="...")``
are the only ways past a SiteScopedManager. This test finds each one in the
server tree and compares it with CROSSINGS, exactly: adding a crossing means
adding it here, where a reviewer sees it. The reason must be a string literal
so it can be read without running the code.
"""
import ast
from pathlib import Path

from django.test import SimpleTestCase

SERVER = Path(__file__).resolve().parent.parent

#: "relative/path.py" -> sorted reasons of the crossings in that file.
CROSSINGS: dict[str, list[str]] = {}


def _is_test(path):
    return path.name.startswith("test") or path.name == "testsupport.py"


def _system_names(tree):
    """Local names bound to vigil.scoping.system by a from-import."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "vigil.scoping":
            names |= {a.asname or a.name for a in node.names if a.name == "system"}
    return names


def _is_crossing(call, system_names):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "unscoped":
        return True
    if isinstance(f, ast.Attribute) and f.attr == "system":
        return isinstance(f.value, ast.Name) and f.value.id == "scoping"
    return isinstance(f, ast.Name) and f.id in system_names


def find_crossings(root=SERVER):
    found, bad = {}, []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if ".venv" in path.parts or "migrations" in path.parts or _is_test(path):
            continue
        if rel == "vigil/scoping.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "unscoped" not in text and "system" not in text:
            continue
        tree = ast.parse(text, filename=rel)
        system_names = _system_names(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_crossing(node, system_names)):
                continue
            reason = next((k.value for k in node.keywords if k.arg == "reason"), None)
            if not (isinstance(reason, ast.Constant) and isinstance(reason.value, str)):
                bad.append(f"{rel}:{node.lineno} has no literal reason")
                continue
            found.setdefault(rel, []).append(reason.value)
    return {k: sorted(v) for k, v in found.items()}, bad


class ScopeCrossingsTests(SimpleTestCase):
    def test_every_crossing_is_pinned(self):
        found, bad = find_crossings()
        self.assertEqual(bad, [], "a crossing must name its reason as a string literal")
        self.maxDiff = None
        self.assertEqual(found, CROSSINGS,
                         "site-scope crossings changed: review each one, then pin it in CROSSINGS")

    def test_the_scanner_sees_both_forms(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "apps").mkdir()
            (root / "apps" / "views.py").write_text(
                "from vigil import scoping\n"
                "a = Host.objects.unscoped(reason=\"agent ingest\")\n"
                "with scoping.system(reason='nightly purge'):\n    pass\n"
                "b = Host.objects.unscoped(reason=why)\n"
                "import platform\nplatform.system()\n",
                encoding="utf-8")
            found, bad = find_crossings(root)
        self.assertEqual(found, {"apps/views.py": ["agent ingest", "nightly purge"]})
        self.assertEqual(len(bad), 1)
        self.assertIn("apps/views.py:5", bad[0])
