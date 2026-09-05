"""Removing a stack used to leave its container alerts firing forever.

A removed container stops emitting metric points, and the resolve branch only
ran when a fresh point arrived saying the image was current — so nothing ever
resolved the alert and it kept pointing at a container that no longer existed.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from apps.alerts.models import Alert, AlertRule
from apps.alerts.tasks import _get_or_create_docker_rule, check_docker_image_updates
from apps.hosts.models import DockerContainer, Host


class DepartedContainerTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="dockerbox", ip_address="10.50.0.2", agent_token="tok",
            status=Host.Status.ONLINE, docker_snapshot_at=now())
        self.rule = _get_or_create_docker_rule()
        self.alert = Alert.objects.create(
            host=self.host, rule=self.rule, state=Alert.State.FIRING,
            severity=AlertRule.Severity.WARNING,
            message="Docker: Container 'gone-app' is running an outdated image (x)",
            metric_value=1.0,
            fix_context={"container_name": "gone-app", "image": "x"})

    def _refresh(self):
        self.alert.refresh_from_db()
        return self.alert.state

    def test_an_alert_for_a_removed_container_resolves(self):
        check_docker_image_updates()
        self.assertEqual(self._refresh(), Alert.State.RESOLVED)

    def test_an_alert_for_a_still_running_container_stays_firing(self):
        DockerContainer.objects.create(
            host=self.host, container_id="c1", name="gone-app", image="x")
        check_docker_image_updates()
        self.assertEqual(self._refresh(), Alert.State.FIRING)

    def test_container_names_compare_case_insensitively(self):
        DockerContainer.objects.create(
            host=self.host, container_id="c1", name="Gone-App", image="x")
        check_docker_image_updates()
        self.assertEqual(self._refresh(), Alert.State.FIRING)

    def test_an_acknowledged_alert_also_resolves(self):
        self.alert.state = Alert.State.ACKNOWLEDGED
        self.alert.save(update_fields=["state"])
        check_docker_image_updates()
        self.assertEqual(self._refresh(), Alert.State.RESOLVED)

    def test_a_host_that_never_reported_docker_keeps_its_alerts(self):
        """An empty container table means "no containers" only when a payload
        actually arrived. Otherwise it means the agent does not report docker,
        and resolving would hide a real alert."""
        self.host.docker_snapshot_at = None
        self.host.save(update_fields=["docker_snapshot_at"])
        check_docker_image_updates()
        self.assertEqual(self._refresh(), Alert.State.FIRING)

    def test_a_stale_docker_snapshot_does_not_resolve_anything(self):
        self.host.docker_snapshot_at = now() - timedelta(hours=3)
        self.host.save(update_fields=["docker_snapshot_at"])
        check_docker_image_updates()
        self.assertEqual(self._refresh(), Alert.State.FIRING)

    def test_an_offline_host_is_not_swept(self):
        self.host.status = Host.Status.OFFLINE
        self.host.save(update_fields=["status"])
        check_docker_image_updates()
        self.assertEqual(self._refresh(), Alert.State.FIRING)


class SnapshotTimestampTests(TestCase):
    def test_an_empty_payload_still_records_a_snapshot(self):
        """The whole fix rests on this: clearing every container has to count
        as a report, or the sweep can never trust the empty table."""
        from django.test import Client

        host = Host.objects.create(
            hostname="h", ip_address="10.50.0.3", agent_token="tok2",
            status=Host.Status.ONLINE, mode="managed")
        Client().post(
            "/api/v1/checkin",
            data={"hostname": "h", "docker_containers": []},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok2")
        host.refresh_from_db()
        self.assertIsNotNone(host.docker_snapshot_at)
