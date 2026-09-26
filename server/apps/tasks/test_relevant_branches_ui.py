"""The editor preview, library card and task-detail state for ``relevant:`` and branches.

Both views are client-rendered: the server only ships the JS modules, so these
SimpleTestCases read the sources the way test_hunt_results_ui.py does —
asserting on the specific snippets that carry the phase's guarantees (the
"Applies when" block, the flow walk, the state labels).
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
TASKS_JS = REPO / "server" / "static" / "js" / "vigil-tasks.js"

#: The one line that escapes every probe param as a k=v pair.
PROBE_PAIR = (
    "Object.entries(params).map(([k, v]) => `${escHtml(String(k))}=${escHtml(String(v))}`)"
    ".join(' ')"
)


def _src() -> str:
    return TASKS_JS.read_text()


class PreviewRelevantTests(SimpleTestCase):

    def test_preview_renders_relevant_escaped(self):
        tasks = _src()
        self.assertIn("preview-relevant", tasks, "the Applies when block is missing")
        self.assertIn("Applies when", tasks)
        # Every op gets its label, probe params go through escHtml.
        self.assertIn("'any of:'", tasks)
        self.assertIn("'none of:'", tasks)
        self.assertIn("'all of:'", tasks)
        self.assertIn(PROBE_PAIR, tasks,
                      "probe params must be escaped k=v pairs")
        self.assertIn("report Not applicable and run nothing", tasks)

    def test_relevant_escaping_survives_comment_stripping(self):
        # The inline-handler scan reads the JS with comments blanked, so the
        # escaping line must stay visible there too — the mutation must turn
        # both scans red.
        code = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                      _src(), flags=re.DOTALL)
        self.assertIn(PROBE_PAIR, code)


class PreviewBranchTests(SimpleTestCase):

    def test_preview_walks_flow(self):
        tasks = _src()
        self.assertIn("preview-branch", tasks, "the branch block is missing")
        self.assertIn("_previewStepHtml(", tasks,
                      "both flow paths must share the step-card helper")
        self.assertIn("preview-use", tasks)
        self.assertIn("its current steps are copied in when you deploy", tasks)
        # The branch header escapes the condition and the use node escapes
        # the referenced task name.
        self.assertIn("if <code>${escHtml(node.if)}</code>", tasks)
        self.assertIn("code>${escHtml(node.use)}</code>", tasks)


class RelevanceSpanGoneTests(SimpleTestCase):

    def test_relevance_span_gone(self):
        tasks = _src()
        self.assertNotIn("spec.relevance", tasks,
                         "the dead relevance span is still in the preview meta row")
        self.assertNotIn("def.relevance", tasks,
                         "the dead relevance span is still in the library card")


class TaskDetailStateTests(SimpleTestCase):

    def test_task_detail_uses_state_labels(self):
        tasks = _src()
        self.assertIn("not_applicable: 'Not applicable'", tasks,
                      "the state label map must name not_applicable")
        self.assertIn("TASK_STATE_LABELS[task.state] || task.state", tasks,
                      "the task-detail card header must label the state")
