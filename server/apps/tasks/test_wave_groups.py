"""Wave groups, and per-machine detail for a wave.

One global wave order could only describe one rollout shape, so a fleet that
patches servers on a different ladder from workstations had nowhere to put the
second. And a wave only ever reported how many hosts failed, never which.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.tasks.models import PatchWave, PatchWaveGroup, Task, TaskDefinition
from apps.tasks.rollout import start_rollout


def _host(name, tags):
    return Host.objects.create(
        hostname=name, ip_address=f"10.70.0.{abs(hash(name)) % 250 + 1}",
        agent_token=f"t-{name}", tags=tags, status=Host.Status.ONLINE,
        mode="managed")


class WaveGroupTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def test_a_wave_created_without_a_group_lands_in_the_default(self):
        wave = PatchWave.objects.create(name="w", order=1, tags=["prod"])
        self.assertIsNotNone(wave.group)
        self.assertTrue(wave.group.is_default)

    def test_the_same_order_may_repeat_across_groups(self):
        servers = PatchWaveGroup.objects.create(name="Servers")
        desktops = PatchWaveGroup.objects.create(name="Desktops")
        PatchWave.objects.create(name="a", order=1, tags=["x"], group=servers)
        PatchWave.objects.create(name="b", order=1, tags=["y"], group=desktops)
        self.assertEqual(PatchWave.objects.filter(order=1).count(), 2)

    def test_the_same_order_still_collides_inside_one_group(self):
        from django.db import IntegrityError

        group = PatchWaveGroup.objects.create(name="Servers")
        PatchWave.objects.create(name="a", order=1, tags=["x"], group=group)
        with self.assertRaises(IntegrityError):
            PatchWave.objects.create(name="b", order=1, tags=["y"], group=group)

    def test_groups_are_listed_with_their_wave_counts(self):
        group = PatchWaveGroup.objects.create(name="Servers")
        PatchWave.objects.create(name="a", order=1, tags=["x"], group=group)
        rows = self.client.get("/api/v1/wave-groups/").json()
        row = next(r for r in rows if r["name"] == "Servers")
        self.assertEqual(row["wave_count"], 1)

    def test_the_default_group_cannot_be_deleted(self):
        PatchWave.objects.create(name="a", order=1, tags=["x"])
        default = PatchWaveGroup.objects.get(is_default=True)
        resp = self.client.delete(f"/api/v1/wave-groups/{default.id}/")
        self.assertEqual(resp.status_code, 400)

    def test_an_unused_group_can_be_deleted(self):
        group = PatchWaveGroup.objects.create(name="Spare")
        resp = self.client.delete(f"/api/v1/wave-groups/{group.id}/")
        self.assertEqual(resp.status_code, 204)

    def test_waves_can_be_listed_for_one_group(self):
        servers = PatchWaveGroup.objects.create(name="Servers")
        PatchWave.objects.create(name="in", order=1, tags=["x"], group=servers)
        PatchWave.objects.create(name="out", order=2, tags=["y"])
        rows = self.client.get(f"/api/v1/waves/?group={servers.id}").json()
        self.assertEqual([r["name"] for r in rows], ["in"])


class RolloutUsesOneLadderTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.definition = TaskDefinition.objects.create(
            name="Patch", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard",
                         "actions": [{"type": "restart_service",
                                      "params": {"service_name": "ssh"}}]})
        self.servers = PatchWaveGroup.objects.create(name="Servers")
        self.wave = PatchWave.objects.create(
            name="canary", order=1, tags=["srv"], group=self.servers,
            validation_hours=0)
        _host("srv1", ["srv"])

    def test_a_rollout_walks_the_group_it_was_given(self):
        rollout = start_rollout(self.definition, user=self.admin,
                                wave_group=self.servers)
        self.assertEqual(rollout.wave_group_id, self.servers.id)
        self.assertEqual(rollout.current_wave_id, self.wave.id)

    def test_a_rollout_ignores_waves_in_other_groups(self):
        other = PatchWaveGroup.objects.create(name="Desktops")
        PatchWave.objects.create(name="earlier", order=0, tags=["srv"],
                                 group=other)
        rollout = start_rollout(self.definition, user=self.admin,
                                wave_group=self.servers)
        self.assertEqual(rollout.current_wave_id, self.wave.id)

    def test_a_group_with_no_enabled_waves_is_refused(self):
        empty = PatchWaveGroup.objects.create(name="Empty")
        with self.assertRaises(ValueError):
            start_rollout(self.definition, user=self.admin, wave_group=empty)


class WaveHostDetailTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.definition = TaskDefinition.objects.create(
            name="Patch", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard",
                         "actions": [{"type": "restart_service",
                                      "params": {"service_name": "ssh"}}]})
        PatchWave.objects.create(name="canary", order=1, tags=["srv"],
                                 validation_hours=0)
        self.host = _host("srv1", ["srv"])
        self.rollout = start_rollout(self.definition, user=self.admin)

    def _rows(self):
        resp = self.client.get(
            f"/api/v1/rollouts/{self.rollout.id}/waves/"
            f"{self.rollout.current_wave_id}/hosts/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_each_dispatched_machine_is_listed_by_name(self):
        body = self._rows()
        self.assertEqual([h["hostname"] for h in body["hosts"]], ["srv1"])
        self.assertEqual(body["total"], 1)

    def test_a_failure_carries_its_output_so_you_can_see_why(self):
        task = Task.objects.get(host=self.host)
        task.state = Task.State.FAILED
        task.result_output = "systemctl: unit ssh.service not found"
        task.save(update_fields=["state", "result_output"])
        body = self._rows()
        row = body["hosts"][0]
        self.assertTrue(row["failed"])
        self.assertIn("not found", row["output"])
        self.assertEqual(body["failed"], 1)

    def test_a_healthy_machine_is_not_marked_failed(self):
        task = Task.objects.get(host=self.host)
        task.state = Task.State.COMPLETED
        task.save(update_fields=["state"])
        body = self._rows()
        self.assertFalse(body["hosts"][0]["failed"])
        self.assertEqual(body["failed"], 0)
