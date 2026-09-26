"""The hunt results page and its entry points from the run-detail modals.

The results view is a client-rendered page: the server only ships the partial
and the JS module, so these SimpleTestCases read the sources the way
test_inline_handlers.py does — asserting on the specific snippets that carry
the phase's guarantees (escaping, the CSV formula guard, the entry buttons).
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
JS = REPO / "server" / "static" / "js"
TEMPLATES = REPO / "server" / "templates"

INLINE = re.compile(r'\bon(click|change|input|submit|error|focus|blur)\s*=')


def _src(name: str) -> str:
    return (JS / name).read_text()


def _tpl(name: str) -> str:
    return (TEMPLATES / name).read_text()


class HuntPagePresenceTests(SimpleTestCase):

    def test_page_is_included_and_script_loaded(self):
        page = _tpl("pages/_hunt_results.html")
        self.assertEqual(page.count('id="page-hunt-results"'), 1)
        dashboard = _tpl("dashboard.html")
        self.assertIn('{% include "pages/_hunt_results.html" %}', dashboard)
        base = _tpl("base.html")
        self.assertIn("js/vigil-hunts.js", base)
        # The script must load after vigil-runhistory.js, whose
        # _runHasHuntSteps() it calls from the run-detail modal.
        self.assertLess(base.index("js/vigil-runhistory.js"), base.index("js/vigil-hunts.js"))
        # The task-detail modal carries the hunt entry point.
        self.assertIn('id="task-detail-actions"', base)
        self.assertIn('id="task-detail-hunt"', base)

    def test_no_inline_handlers_in_hunt_page(self):
        offenders = [
            line.strip() for line in _tpl("pages/_hunt_results.html").splitlines()
            if INLINE.search(line)
        ]
        self.assertEqual(offenders, [], "\n".join(offenders))
        # The generated markup in the JS module is covered by
        # test_inline_handlers.py's scan of every vigil-*.js file; this
        # asserts the module does not even write handler attributes.
        hunts = _src("vigil-hunts.js")
        self.assertNotIn("onclick=", hunts)
        self.assertNotIn("onchange=", hunts)
        self.assertNotIn("oninput=", hunts)


class HuntResultsModuleTests(SimpleTestCase):

    def test_open_hunt_results_navigates_and_fetches(self):
        hunts = _src("vigil-hunts.js")
        self.assertIn("async function openHuntResults(runId)", hunts)
        self.assertIn("navigateTo('hunt-results')", hunts)
        self.assertIn("openHuntResults(", _src("vigil-runhistory.js"))
        self.assertIn("openHuntResults(", _src("vigil-tasks.js"))

    def test_matches_are_escaped(self):
        hunts = _src("vigil-hunts.js")
        # Every dynamic matches cell goes through escHtml on String(value).
        self.assertIn("escHtml(String(", hunts)
        self.assertIn("escHtml(String(m[c] ?? ''))", hunts)
        # The fixed identity columns are present in the header and cells.
        self.assertIn("hostname', 'step_id', 'evidence_type'", hunts)

    def test_csv_export_guards_formulas(self):
        hunts = _src("vigil-hunts.js")
        # Cells starting with a formula char get a ' prefix before quoting.
        self.assertIn("/^[=+\\-@]/", hunts)
        self.assertIn("s = \"'\" + s", hunts)
        # RFC 4180: quote every field, double embedded quotes.
        self.assertIn('s.replace(/"/g, \'""\')', hunts)
        # Downloaded via a Blob and a temporary <a download>.
        self.assertIn("new Blob(", hunts)
        self.assertIn("a.download", hunts)

    def test_run_detail_links_hunt_results(self):
        rh = _src("vigil-runhistory.js")
        self.assertIn('id="rd-hunt"', rh)
        self.assertIn("_runHasHuntSteps(run)", rh)
        self.assertIn("startsWith('hunt_')", _src("vigil-hunts.js"))
        # The button is only rendered when a hunt step exists, and its
        # handler is assigned in JS — the modal body carries no inline
        # handler (covered by test_inline_handlers.py).
        self.assertIn("huntBtnEl.onclick", rh)

    def test_task_detail_links_hunt_results_and_shows_step_results(self):
        tasks = _src("vigil-tasks.js")
        base = _tpl("base.html")
        self.assertIn('id="task-detail-hunt"', base)
        self.assertIn("task-detail-actions", tasks)
        self.assertIn("huntBtn.onclick", tasks)
        self.assertIn("closeTaskDetail()", tasks)
        self.assertIn("openHuntResults(run.id)", tasks)
        # The modal starts hidden and is only shown for runs with hunts.
        self.assertIn("actionsEl.hidden = true", tasks)
        self.assertIn("actionsEl.hidden = !_runHasHuntSteps(run)", tasks)
        # M4 per-step results now render in the task-detail cards too.
        self.assertIn("_stepResultsHtml(task.result_data)", tasks)

    def test_matches_cells_use_escaped_string_interpolation(self):
        hunts = _src("vigil-hunts.js")
        self.assertIn(
            '<td>${escHtml(String(m[c] ?? \'\'))}</td>',
            hunts,
            "every matches cell must be escHtml(String(v ?? ''))",
        )
