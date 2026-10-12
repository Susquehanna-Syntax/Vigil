"""The bundled template playbooks: they parse, they resolve, they can be used.

A library template is a promise made in a file: it says "these built-in tasks,
in this order" and the Playbooks page offers it to an operator who has no way to
know whether the tasks it names still exist. The resolution tests are what keep
that honest — a renamed built-in task turns this suite red instead of silently
offering a template that refuses to be created.
"""

from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.playbooks.community_yaml import parse, resolve_steps
from apps.playbooks.models import Playbook
from apps.tasks.models import TaskDefinition

LIBRARY_DIR = Path(__file__).resolve().parent / "library"


def _builtins():
    return TaskDefinition.objects.filter(owner=None, archived_at__isnull=True)


class LibraryFileTests(TestCase):
    def test_every_template_parses_and_resolves_against_the_builtins(self):
        seen = 0
        for path in sorted(LIBRARY_DIR.glob("*.yaml")):
            with self.subTest(template=path.stem):
                parsed = parse(path.read_text())
                resolved = resolve_steps(parsed["steps"], _builtins())
                self.assertEqual(len(resolved), len(parsed["steps"]))
                # Steps are named by slug and ordered, so a step that resolves
                # to a task the file did not mean is visible here.
                for step in resolved:
                    self.assertIsNotNone(step["definition"].id)
                seen += 1
        self.assertGreaterEqual(seen, 5, "the library lost templates")

    def test_each_template_says_what_it_shows_you(self):
        for path in sorted(LIBRARY_DIR.glob("*.yaml")):
            with self.subTest(template=path.stem):
                description = parse(path.read_text())["description"]
                self.assertIn("shows you", description,
                              "a template must say what it teaches")


class LibraryUseTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("la", password="pw")
        UserProfile.objects.create(user=self.admin, role=Role.ADMIN)
        self.client.force_login(self.admin)

    def _use(self, template_id, body=None):
        return self.client.post(
            f"/api/v1/playbooks/library/{template_id}/use/",
            body or {}, content_type="application/json")

    def test_list_names_the_steps_and_the_inputs(self):
        body = self.client.get("/api/v1/playbooks/library/").json()
        by_id = {r["id"]: r for r in body["results"]}
        self.assertIn("restart-service-and-check", by_id)
        restart = by_id["restart-service-and-check"]
        self.assertEqual(restart["steps"],
                         ["Restart a service", "Check a service", "Start a service"])
        self.assertEqual([i["id"] for i in restart["inputs"]], ["service"])
        self.assertTrue(by_id["scan-with-trivy"]["allow_high_risk"])

    def test_use_creates_the_playbook_with_its_steps_and_inputs(self):
        r = self._use("restart-service-and-check")
        self.assertEqual(r.status_code, 201, r.content[:400])
        playbook = Playbook.objects.get(pk=r.json()["id"])
        self.assertEqual([s.definition.name for s in playbook.steps.order_by("order")],
                         ["Restart a service", "Check a service", "Start a service"])
        first = playbook.steps.order_by("order").first()
        self.assertEqual(first.inputs, {"service": "${{ inputs.service }}"})
        check = playbook.steps.get(step_id="check")
        self.assertEqual(check.on_failure, "continue")
        self.assertTrue(playbook.flow, "the branch must survive into the playbook")

    def test_using_a_template_twice_numbers_the_copy(self):
        first = self._use("keep-apps-up-to-date").json()["name"]
        second = self._use("keep-apps-up-to-date").json()["name"]
        third = self._use("keep-apps-up-to-date").json()["name"]
        self.assertEqual([first, second, third],
                         ["Keep apps up to date", "Keep apps up to date (2)",
                          "Keep apps up to date (3)"])

    def test_an_operator_cannot_use_a_template(self):
        operator = get_user_model().objects.create_user("op", password="pw")
        UserProfile.objects.create(user=operator, role=Role.OPERATOR)
        self.client.force_login(operator)
        self.assertEqual(self._use("clean-up-container-host").status_code, 403)

    def test_an_unknown_template_is_a_404(self):
        self.assertEqual(self._use("nothing-like-this").status_code, 404)

    def test_the_high_risk_template_needs_the_same_totp_as_the_yaml_import(self):
        r = self._use("scan-with-trivy")
        self.assertEqual(r.status_code, 403, r.content[:400])
        self.assertTrue(r.json().get("needs_totp"))
        self.assertFalse(Playbook.objects.exists(),
                         "a refused template must not leave a playbook behind")
