"""The container Update button and the deploy-modal prefill.

An outdated container offers an Update button that opens the normal deploy
modal on the built-in "Update container" task with the host and container name
prefilled, so the update goes through the same TOTP approval and signing as
any other task. The AI "Suggest fix" path for containers is gone.

Vigil has no JS test runner; these SimpleTestCases read the JS sources the way
test_inline_handlers.py does.
"""

from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
JS = REPO / "server" / "static" / "js"


def _src(name: str) -> str:
    return (JS / name).read_text()


class AiDockerFixGoneTests(SimpleTestCase):

    def test_the_ai_docker_fix_is_gone(self):
        ai = _src("vigil-ai.js")
        self.assertFalse("_staticDockerFix" in ai, "_staticDockerFix is still in vigil-ai.js")
        self.assertFalse("suggestFixForContainer" in ai, "suggestFixForContainer is still in vigil-ai.js")
        self.assertTrue("function suggestFixForVuln" in ai, "suggestFixForVuln was lost")
        self.assertTrue("function suggestFixForAlert" in ai, "suggestFixForAlert was lost")


class UpdateButtonTests(SimpleTestCase):

    def test_an_outdated_container_offers_update(self):
        monitor = _src("vigil-monitor.js")
        self.assertTrue("c.outdated ?" in monitor, "the Update button is not gated on c.outdated")
        self.assertTrue("data-ctr-update" in monitor, "the data-ctr-update hook is missing")
        self.assertTrue("openUpdateContainer(" in monitor, "the button does not call openUpdateContainer")
        self.assertFalse("Suggest fix" in monitor, "the old Suggest fix button is still in vigil-monitor.js")


class OpenUpdateContainerTests(SimpleTestCase):

    def test_update_opens_the_builtin_task_prefilled(self):
        deploy = _src("vigil-deploy.js")
        self.assertTrue("function openUpdateContainer" in deploy, "openUpdateContainer is missing")
        self.assertTrue("'Update container'" in deploy, "the built-in task name is not looked up")
        self.assertTrue("scope=community" in deploy, "the definitions are not fetched from the community scope")
        self.assertTrue("inputs: { container_name: containerName }" in deploy, "the container name is not prefilled")


class DeployModalPrefillTests(SimpleTestCase):

    def test_the_deploy_modal_accepts_a_prefill(self):
        deploy = _src("vigil-deploy.js")
        self.assertTrue("openDeployModal(definitionId, prefill)" in deploy, "openDeployModal does not take a prefill argument")
        self.assertTrue("prefill.inputs" in deploy, "prefill.inputs is not consumed")
        self.assertTrue("prefill.hostId" in deploy, "prefill.hostId is not consumed")
