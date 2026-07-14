from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import DockerContainer, Host


def _container(name, stack="", cpu=None):
    return {
        "container_id": "id-" + name,
        "name": name,
        "image": "nginx:1",
        "state": "running",
        "status": "Up 2 hours",
        "stack": stack,
        "service": name,
        "cpu_percent": cpu,
        "mem_usage_bytes": 1048576,
        "mem_limit_bytes": 2097152,
        "mem_percent": 50.0,
        "ports": [{"private": 80, "public": 8080}],
    }


class DockerCheckinTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="h", agent_token="tok-" + "z" * 32, status=Host.Status.ONLINE,
        )

    def _checkin(self, body):
        return self.client.post(
            "/api/v1/checkin", body, format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}",
        )

    def test_checkin_ingests_docker_containers(self):
        resp = self._checkin({
            "docker_containers": [
                _container("web", stack="shop", cpu=3.5),
                _container("db", stack="shop", cpu=1.0),
            ],
        })
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", None))
        rows = list(DockerContainer.objects.filter(host=self.host).order_by("name"))
        self.assertEqual(len(rows), 2)
        db, web = rows
        self.assertEqual(web.name, "web")
        self.assertEqual(web.stack, "shop")
        self.assertEqual(web.cpu_percent, 3.5)
        self.assertEqual(web.ports, [{"private": 80, "public": 8080}])

    def test_checkin_replaces_container_snapshot(self):
        self._checkin({"docker_containers": [_container("A"), _container("B")]})
        self._checkin({"docker_containers": [_container("C")]})
        names = set(DockerContainer.objects.filter(host=self.host).values_list("name", flat=True))
        self.assertEqual(names, {"C"})

    def test_checkin_without_docker_key_preserves_rows(self):
        DockerContainer.objects.create(host=self.host, container_id="x", name="keep")
        self._checkin({"hostname": "h"})
        self.assertEqual(DockerContainer.objects.filter(host=self.host).count(), 1)

    def test_checkin_empty_docker_list_clears_rows(self):
        DockerContainer.objects.create(host=self.host, container_id="x", name="gone")
        self._checkin({"docker_containers": []})
        self.assertEqual(DockerContainer.objects.filter(host=self.host).count(), 0)

    def test_checkin_skips_malformed_container(self):
        resp = self._checkin({
            "docker_containers": [{"image": "x"}, "notadict", _container("ok")],
        })
        self.assertEqual(resp.status_code, 200)
        rows = DockerContainer.objects.filter(host=self.host)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().name, "ok")


class DockerReadApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.client.force_authenticate(self.user)
        self.h1 = Host.objects.create(
            hostname="h1", agent_token="tok-" + "a" * 32, status=Host.Status.ONLINE,
        )
        self.h2 = Host.objects.create(
            hostname="h2", agent_token="tok-" + "b" * 32, status=Host.Status.ONLINE,
        )
        DockerContainer.objects.create(
            host=self.h1, container_id="1", name="web", stack="shop",
        )
        DockerContainer.objects.create(
            host=self.h1, container_id="2", name="cache", stack="infra",
        )
        DockerContainer.objects.create(
            host=self.h2, container_id="3", name="lonely",
        )

    def test_host_containers_endpoint(self):
        resp = self.client.get(f"/api/v1/hosts/{self.h1.id}/containers/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)
        # Meta ordering is by stack then name: "infra" before "shop".
        self.assertEqual(resp.data[0]["stack"], "infra")

    def test_docker_overview_endpoint(self):
        resp = self.client.get("/api/v1/hosts/docker/overview/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 3)
        hostnames = {r["host_hostname"] for r in resp.data}
        self.assertEqual(hostnames, {"h1", "h2"})

    def test_container_endpoints_require_auth(self):
        anon = APIClient()
        self.assertEqual(
            anon.get(f"/api/v1/hosts/{self.h1.id}/containers/").status_code, 403,
        )
        self.assertEqual(anon.get("/api/v1/hosts/docker/overview/").status_code, 403)
