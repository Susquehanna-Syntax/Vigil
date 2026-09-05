"""Retiring a task or a playbook without deleting it.

Neither can be deleted: a playbook or automation still pointing at a definition
would silently lose a step, and deleting a playbook discards its run history
along with it. Archiving takes them out of the working set instead.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.playbooks.models import Playbook, PlaybookStep, reconcile
from apps.tasks.models import TaskDefinition


def _definition(name="Step"):
    return TaskDefinition.objects.create(
        name=name, risk_level="standard", yaml_source="",
        parsed_spec={"risk": "standard",
                     "actions": [{"type": "restart_service",
                                  "params": {"service_name": "ssh"}}]})


class PlaybookArchiveTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.playbook = Playbook.objects.create(
            name="Retired", created_by=self.admin, target_tags=["prod"],
            completion_tag="done", auto_enroll=True)
        PlaybookStep.objects.create(
            playbook=self.playbook, definition=_definition(), order=0)

    def _archive(self, **body):
        return self.client.post(f"/api/v1/playbooks/{self.playbook.id}/archive/",
                                body, content_type="application/json")

    def test_archiving_hides_it_from_the_default_list(self):
        self._archive()
        names = [r["name"] for r in self.client.get("/api/v1/playbooks/").json()]
        self.assertNotIn("Retired", names)

    def test_the_archived_list_shows_it(self):
        self._archive()
        names = [r["name"]
                 for r in self.client.get("/api/v1/playbooks/?archived=1").json()]
        self.assertEqual(names, ["Retired"])

    def test_archiving_turns_auto_enrolment_off(self):
        self._archive()
        self.playbook.refresh_from_db()
        self.assertFalse(self.playbook.auto_enroll)

    def test_an_archived_playbook_is_never_reconciled(self):
        Host.objects.create(hostname="p", ip_address="10.60.0.2",
                            agent_token="t", tags=["prod"], mode="managed",
                            status=Host.Status.ONLINE)
        self._archive()
        self.assertEqual(reconcile(), 0)

    def test_restoring_puts_it_back(self):
        self._archive()
        self._archive(restore=True)
        names = [r["name"] for r in self.client.get("/api/v1/playbooks/").json()]
        self.assertIn("Retired", names)

    def test_restoring_does_not_silently_re_enable_auto_enrolment(self):
        self._archive()
        self._archive(restore=True)
        self.playbook.refresh_from_db()
        self.assertFalse(self.playbook.auto_enroll)

    def test_the_playbook_still_exists_with_its_steps(self):
        self._archive()
        self.playbook.refresh_from_db()
        self.assertEqual(self.playbook.steps.count(), 1)


class DefinitionArchiveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="op", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.definition = _definition("Old task")
        self.definition.owner = self.user
        self.definition.save(update_fields=["owner"])

    def _archive(self, **body):
        return self.client.post(
            f"/api/v1/tasks/definitions/{self.definition.id}/archive/",
            body, content_type="application/json")

    def test_archiving_hides_it_from_the_default_list(self):
        self._archive()
        names = [r["name"]
                 for r in self.client.get("/api/v1/tasks/definitions/").json()]
        self.assertNotIn("Old task", names)

    def test_the_archived_list_shows_it(self):
        self._archive()
        names = [r["name"] for r in self.client.get(
            "/api/v1/tasks/definitions/?archived=1").json()]
        self.assertEqual(names, ["Old task"])

    def test_restoring_puts_it_back(self):
        self._archive()
        self._archive(restore=True)
        names = [r["name"]
                 for r in self.client.get("/api/v1/tasks/definitions/").json()]
        self.assertIn("Old task", names)

    def test_a_playbook_step_survives_archiving_its_definition(self):
        """The reason archiving exists rather than deletion."""
        playbook = Playbook.objects.create(name="Uses it", created_by=self.user)
        PlaybookStep.objects.create(
            playbook=playbook, definition=self.definition, order=0)
        self._archive()
        self.assertEqual(playbook.steps.first().definition_id,
                         self.definition.id)
