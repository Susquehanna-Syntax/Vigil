"""M11 13: an update records the image it replaced; a rollback marks the
container; its next update clears the mark."""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.tasks.models import Task, TaskDefinition

from .models import ContainerImageHistory, ContainerRollback, DockerContainer, Host


class RollbackRecordingTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="dock", agent_token="rbtok", status=Host.Status.ONLINE,
                                        mode="managed")
        DockerContainer.objects.create(host=self.host, container_id="c1", name="jellyfin",
                                       image="jellyfin/jellyfin:latest", image_id="sha256:old",
                                       image_digest="jellyfin/jellyfin@sha256:d1")
        self.n = 0

    def _complete(self, action, params, result):
        self.n += 1
        task = Task.objects.create(host=self.host, action="_script", nonce=f"{self.n}" * 64,
                                   state=Task.State.DISPATCHED,
                                   params={"steps": [{"id": "s", "action": action, "params": params}]})
        resp = self.client.post("/api/v1/tasks/result/", data=json.dumps(
            {"task_id": str(task.id), "state": "completed", "output": "ok",
             "steps": [{"id": "s", "status": "ok", "result": result}]}),
            content_type="application/json", HTTP_AUTHORIZATION="Bearer rbtok")
        self.assertEqual(resp.status_code, 200, resp.content)

    def _containers(self):
        user = get_user_model().objects.create_user(f"u{self.n}", password="x")
        UserProfile.objects.create(user=user, role=Role.VIEWER)
        api = APIClient()
        api.force_authenticate(user)
        return {c["name"]: c for c in api.get(f"/api/v1/hosts/{self.host.id}/containers/").json()}

    def test_update_rollback_update(self):
        self._complete("update_container", {"container_name": "jellyfin"},
                       {"updated": True, "old_image_id": "sha256:old", "new_image_id": "sha256:new"})
        h = ContainerImageHistory.objects.get()
        self.assertEqual((h.image_id, h.image_digest), ("sha256:old", "jellyfin/jellyfin@sha256:d1"))
        self.assertEqual(self._containers()["jellyfin"]["previous_image"], "jellyfin/jellyfin@sha256:d1")

        self._complete("container_rollback", {"container_name": "jellyfin", "image": h.image_digest},
                       {"rolled_back": True, "image": h.image_digest})
        self.assertEqual(self._containers()["jellyfin"]["rolled_back"], "jellyfin/jellyfin@sha256:d1")

        self._complete("update_container", {"container_name": "jellyfin"},
                       {"updated": False, "old_image_id": "sha256:new", "new_image_id": "sha256:new"})
        self.assertFalse(ContainerRollback.objects.exists(), "the next update clears the rollback")
        self.assertEqual(ContainerImageHistory.objects.count(), 1, "an unchanged image records nothing")

    def test_seeded_template(self):
        from apps.tasks.spec import parse_and_validate
        d = TaskDefinition.objects.get(name="Roll back container", owner=None)
        spec = parse_and_validate(d.yaml_source)
        self.assertEqual(spec["actions"][0]["type"], "container_rollback")
        self.assertEqual([i["id"] for i in spec["inputs"]], ["container_name", "image"])
