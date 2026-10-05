"""M11 04: compose stacks found from containers at check-in, and served per host."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile

from .models import ContainerStack, DockerContainer, Host


def _ctr(name, stack="", state="running", **extra):
    return {"container_id": name, "name": name, "image": "nginx:latest", "state": state,
            "stack": stack, "service": name, **extra}


class StackSyncTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="dock", agent_token="docktok",
                                        status=Host.Status.ONLINE, mode="managed",
                                        container_engines=[{"kind": "podman"}])

    def _checkin(self, containers):
        resp = self.client.post("/api/v1/checkin", {"hostname": "dock", "docker_containers": containers},
                                content_type="application/json", HTTP_AUTHORIZATION="Bearer docktok")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_stacks_follow_their_containers(self):
        self._checkin([
            _ctr("jellyfin", "media", config_files="/opt/media/compose.yaml,/opt/media/override.yaml",
                 working_dir="/opt/media", image_digest="jellyfin@sha256:d1", restart_policy="always",
                 config_hash="h1"),
            _ctr("sonarr", "media", state="exited"),
            _ctr("lonely"),
        ])
        stack = ContainerStack.objects.get(host=self.host)
        self.assertEqual((stack.project, stack.container_count, stack.running_count),
                         ("media", 2, 1))
        self.assertEqual(stack.config_files, ["/opt/media/compose.yaml", "/opt/media/override.yaml"])
        self.assertEqual((stack.working_dir, stack.ownership, stack.engine),
                         ("/opt/media", "external", "podman"))
        row = DockerContainer.objects.get(name="jellyfin")
        self.assertEqual((row.image_digest, row.restart_policy, row.config_hash),
                         ("jellyfin@sha256:d1", "always", "h1"))

    def test_external_stacks_go_managed_ones_stay(self):
        self._checkin([_ctr("a", "one"), _ctr("b", "two")])
        ContainerStack.objects.filter(project="two").update(ownership="managed")
        self._checkin([])
        self.assertEqual(list(ContainerStack.objects.values_list("project", "container_count")),
                         [("two", 0)])

    def test_stacks_endpoint_is_scoped(self):
        self._checkin([_ctr("a", "one")])
        user = get_user_model().objects.create_user("v", password="x")
        UserProfile.objects.create(user=user, role=Role.VIEWER)
        client = APIClient()
        client.force_authenticate(user)
        body = client.get(f"/api/v1/hosts/{self.host.id}/stacks/").json()
        self.assertEqual([s["project"] for s in body], ["one"])
        self.assertEqual(client.get("/api/v1/hosts/00000000-0000-4000-8000-000000000000/stacks/").status_code, 404)
