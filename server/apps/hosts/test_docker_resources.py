"""Images, volumes and networks (14.3): check-in ingest, the per-host API
with what uses each object, and the disk-usage reading from a task result."""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import DockerContainer, DockerImage, Host, HostDockerUsage

RESOURCES = {
    "images": [
        {"id": "sha256:" + "a" * 64, "tags": ["nginx:1.25"], "size": 1000, "created": 1700000000},
        {"id": "sha256:" + "b" * 64, "tags": [], "size": 500, "created": 1700000000},
    ],
    "volumes": [{"name": "shop_db", "driver": "local", "stack": "shop"},
                {"name": "orphan", "driver": "local"}],
    "networks": [{"id": "n1", "name": "shop_default", "driver": "bridge", "subnets": ["172.18.0.0/16"],
                  "stack": "shop"},
                 {"id": "n2", "name": "bridge", "driver": "bridge"}],
}

CONTAINER = {
    "container_id": "c1", "name": "db", "image": "nginx:1.25", "state": "running",
    "image_id": "sha256:" + "a" * 64, "stack": "shop",
    "networks": [{"name": "shop_default", "ip": "172.18.0.2", "network_id": "n1"}],
    "mounts": [{"type": "volume", "name": "shop_db", "destination": "/var/lib/mysql", "rw": True},
               {"type": "bind", "source": "/etc/x", "destination": "/x", "rw": False, "junk": {"no": 1}}],
}


class DockerResourceTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="tok-" + "z" * 32, status=Host.Status.ONLINE)
        self.agent = APIClient()
        user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(user)

    def _checkin(self, body):
        return self.agent.post("/api/v1/checkin", body, format="json",
                               HTTP_AUTHORIZATION=f"Bearer {self.host.raw_agent_token}")

    def test_checkin_stores_lists_and_edges_and_the_api_says_what_uses_what(self):
        self.assertEqual(self._checkin({"docker_containers": [CONTAINER], "docker_resources": RESOURCES})
                         .status_code, 200)
        c = DockerContainer.objects.get()
        self.assertEqual(c.networks[0]["ip"], "172.18.0.2")
        self.assertNotIn("junk", c.mounts[1], "only flat values are kept")
        body = self.api.get(f"/api/v1/hosts/{self.host.id}/docker/").json()
        images = {i["short_id"]: i for i in body["images"]}
        self.assertEqual(images["a" * 12]["used_by"], ["db"])
        self.assertTrue(images["b" * 12]["dangling"])
        volumes = {v["name"]: v for v in body["volumes"]}
        self.assertEqual(volumes["shop_db"]["used_by"][0]["destination"], "/var/lib/mysql")
        self.assertEqual(volumes["orphan"]["used_by"], [])
        nets = {n["name"]: n for n in body["networks"]}
        self.assertEqual(nets["shop_default"]["containers"], [{"container": "db", "ip": "172.18.0.2",
                                                               "state": "running"}])
        self.assertTrue(nets["bridge"]["builtin"])
        self.assertIsNone(body["usage"])

    def test_an_absent_key_keeps_the_last_lists(self):
        self._checkin({"docker_resources": RESOURCES})
        self._checkin({"metrics": []})
        self.assertEqual(DockerImage.objects.count(), 2)

    def test_a_disk_usage_task_result_is_stored(self):
        from apps.tasks.models import Task
        from apps.tasks.views import _maybe_ingest_docker_usage
        task = Task.objects.create(host=self.host, action="docker_disk_usage", params={})
        output = "[OK] usage: " + json.dumps({
            "kind": "docker_disk_usage", "images_size": 5000, "images_reclaimable": 2000,
            "volumes_size": 1000, "volumes_reclaimable": 300, "build_cache_size": -5,
            "volumes": [{"name": "shop_db", "size": 700, "refs": 1}, {"name": "orphan", "size": None}],
            "images": [{"id": "sha256:" + "a" * 64, "containers": 1}]})
        _maybe_ingest_docker_usage(task, output)
        usage = HostDockerUsage.objects.get(host=self.host)
        self.assertEqual((usage.images_reclaimable, usage.volumes_size, usage.build_cache_size), (2000, 1000, 0))
        self._checkin({"docker_resources": RESOURCES})
        body = self.api.get(f"/api/v1/hosts/{self.host.id}/docker/").json()
        self.assertEqual({v["name"]: v["size"] for v in body["volumes"]}, {"orphan": None, "shop_db": 700})
        self.assertEqual(body["usage"]["volumes_reclaimable"], 300)

    def test_other_tasks_are_left_alone(self):
        from apps.tasks.models import Task
        from apps.tasks.views import _maybe_ingest_docker_usage
        task = Task.objects.create(host=self.host, action="restart_container", params={})
        _maybe_ingest_docker_usage(task, json.dumps({"kind": "docker_disk_usage", "images_size": 1}))
        self.assertFalse(HostDockerUsage.objects.exists())

    def test_builtin_tasks_are_seeded_with_their_risk(self):
        from apps.tasks.models import TaskDefinition
        risks = dict(TaskDefinition.objects.filter(owner=None, name__in=[
            "Measure container storage", "Prune unused images", "Remove volume", "Prune unused volumes",
            "Prune unused networks"]).values_list("name", "risk_level"))
        self.assertEqual(risks, {"Measure container storage": "low", "Prune unused images": "standard",
                                 "Remove volume": "high", "Prune unused volumes": "high",
                                 "Prune unused networks": "standard"})
