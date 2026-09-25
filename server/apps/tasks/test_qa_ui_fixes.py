"""UI fixes from the 2026-09-24 browser QA of the deploy modal, editor and run detail.

Found by driving the real pages: a task's inputs were hidden behind an
"Options" tab; the browser's own validation silently refused to submit when
the bad field was on that hidden tab; number inputs refused decimals; the run
detail showed the host's UUID and ran step results together; and the editor
kept showing the last valid preview next to a new error. Vigil has no JS test
runner, so these pin the fixes in the source.
"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

JS = Path(settings.BASE_DIR) / "static" / "js"
TEMPLATES = Path(settings.BASE_DIR) / "templates"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class DeployModalQaTests(SimpleTestCase):
    def test_form_does_its_own_validation(self):
        base = _read(TEMPLATES / "base.html")
        self.assertTrue('data-submit="submitDeploy" data-a1="$event" novalidate' in base,
                        "deploy form must be novalidate")
        deploy = _read(JS / "vigil-deploy.js")
        self.assertTrue("_firstInvalidDeployInput()" in deploy, "submitDeploy must name the bad input")

    def test_inputs_tab_is_labelled_and_opened_first(self):
        self.assertTrue('id="deploy-tab-options" style="display:none;">Inputs</button>'
                        in _read(TEMPLATES / "base.html"), "tab should read Inputs")
        self.assertTrue("setDeployTab('options')" in _read(JS / "vigil-deploy.js"),
                        "a task with inputs should open on them")

    def test_number_inputs_accept_decimals(self):
        self.assertTrue("control.step = 'any'" in _read(JS / "vigil-deploy.js"), "number step must be any")


class RunDetailAndEditorQaTests(SimpleTestCase):
    def test_run_detail_shows_hostname_and_a_step_table(self):
        src = _read(JS / "vigil-runhistory.js")
        self.assertTrue("t.host_hostname" in src, "the API field is host_hostname")
        self.assertTrue("<th>Step</th><th>Status</th><th>Result</th>" in src, "step results as a table")

    def test_editor_dims_a_stale_preview(self):
        self.assertTrue("classList.add('preview-stale')" in _read(JS / "vigil-tasks.js"),
                        "an invalid YAML must dim the last valid preview")
