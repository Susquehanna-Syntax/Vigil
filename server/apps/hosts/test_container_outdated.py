from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.alerts.models import Alert
from apps.alerts.tasks import _get_or_create_docker_rule
from apps.hosts.models import DockerContainer, Host


class ContainerOutdatedTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = get_user_model().objects.create_user(
            "root", password="pw", is_staff=True
        )
        self.client.force_login(self.admin)
        self.host = Host.objects.create(
            hostname="h",
            agent_token="tok-" + "c" * 32,
            status=Host.Status.ONLINE,
        )
        DockerContainer.objects.create(
            host=self.host, container_id="1", name="web", stack="shop"
        )
        DockerContainer.objects.create(
            host=self.host, container_id="2", name="db", stack="shop"
        )

    def _alert(self, state):
        rule = _get_or_create_docker_rule()
        Alert.objects.create(
            host=self.host,
            rule=rule,
            severity="warning",
            message="x",
            state=state,
            fix_context={"container_name": "web", "image": "nginx:alpine"},
        )

    def _outdated_flags(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return {row["name"]: row["outdated"] for row in resp.json()}

    def test_a_container_with_an_open_alert_is_outdated(self):
        self._alert(Alert.State.FIRING)
        flags = self._outdated_flags(f"/api/v1/hosts/{self.host.id}/containers/")
        self.assertIs(flags["web"], True)
        self.assertIs(flags["db"], False)

    def test_an_acknowledged_alert_still_counts(self):
        self._alert(Alert.State.ACKNOWLEDGED)
        flags = self._outdated_flags(f"/api/v1/hosts/{self.host.id}/containers/")
        self.assertIs(flags["web"], True)

    def test_a_resolved_alert_is_not_outdated(self):
        self._alert(Alert.State.RESOLVED)
        flags = self._outdated_flags(f"/api/v1/hosts/{self.host.id}/containers/")
        self.assertIs(flags["web"], False)

    def test_the_overview_marks_outdated_containers(self):
        self._alert(Alert.State.FIRING)
        flags = self._outdated_flags("/api/v1/hosts/docker/overview/")
        self.assertIs(flags["web"], True)
        self.assertIs(flags["db"], False)
