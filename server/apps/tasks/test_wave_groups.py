"""Wave groups, and per-machine detail for a wave.

A group is a tag: everything else in Vigil selects things that way, and a wave
genuinely belongs to more than one ladder — a Canary wave is often the first
rung of both the server and the workstation rollout. And a wave only ever
reported how many hosts failed, never which.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.hosts.models import Host
from apps.tasks.models import PatchWave, Task, TaskDefinition
from apps.tasks.rollout import start_rollout


def _host(name, tags):
    return Host.objects.create(
        hostname=name, ip_address=f"10.70.0.{abs(hash(name)) % 250 + 1}",
        agent_token=f"t-{name}", tags=tags, status=Host.Status.ONLINE,
        mode="managed")


def _wave(name, order, tags, groups=(), **kw):
    wave = PatchWave.objects.create(
        name=name, order=order, tags=tags, group_tags=list(groups), **kw)
    return wave


class WaveGroupTaggingTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def test_a_wave_can_be_tagged_into_a_group(self):
        wave = _wave("canary", 1, ["srv"], groups=["Servers"])
        self.assertEqual(wave.group_tags, ["Servers"])
        self.assertEqual([t.key for t in wave.group_tag_rows.all()], ["servers"])

    def test_a_wave_can_sit_on_two_ladders_at_once(self):
        wave = _wave("canary", 1, ["srv"], groups=["Servers", "Desktops"])
        self.assertEqual(
            sorted(t.key for t in wave.group_tag_rows.all()),
            ["desktops", "servers"])

    def test_a_wave_with_no_group_tags_is_ungrouped(self):
        wave = _wave("plain", 1, ["srv"])
        self.assertEqual(wave.group_tags, [])
        self.assertFalse(wave.group_tag_rows.exists())

    def test_the_same_order_may_repeat_across_ladders(self):
        _wave("a", 1, ["x"], groups=["Servers"])
        _wave("b", 1, ["y"], groups=["Desktops"])
        self.assertEqual(PatchWave.objects.filter(order=1).count(), 2)

    def test_the_api_refuses_a_duplicate_order_inside_one_ladder(self):
        _wave("a", 1, ["x"], groups=["Servers"])
        resp = self.client.post("/api/v1/waves/", {
            "name": "b", "order": 1, "tags": ["y"], "group_tags": ["Servers"],
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Servers", str(resp.json()["order"]))

    def test_the_api_allows_the_same_order_on_a_different_ladder(self):
        _wave("a", 1, ["x"], groups=["Servers"])
        resp = self.client.post("/api/v1/waves/", {
            "name": "b", "order": 1, "tags": ["y"], "group_tags": ["Desktops"],
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_the_api_refuses_a_duplicate_order_among_ungrouped_waves(self):
        _wave("a", 1, ["x"])
        resp = self.client.post("/api/v1/waves/", {
            "name": "b", "order": 1, "tags": ["y"],
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_group_matching_folds_case(self):
        _wave("a", 1, ["x"], groups=["Servers"])
        rows = self.client.get("/api/v1/waves/?group=servers").json()
        self.assertEqual([r["name"] for r in rows], ["a"])

    def test_the_group_list_is_derived_from_the_tags_in_use(self):
        _wave("a", 1, ["x"], groups=["Servers"])
        _wave("b", 2, ["y"], groups=["Servers"])
        _wave("c", 3, ["z"], groups=["Desktops"])
        _wave("loose", 4, ["w"])
        body = self.client.get("/api/v1/wave-groups/").json()
        by_tag = {g["tag"]: g for g in body["groups"]}
        self.assertEqual(by_tag["Servers"]["waves"], 2)
        self.assertEqual(by_tag["Desktops"]["waves"], 1)
        self.assertEqual(body["ungrouped_waves"], 1)


class RolloutWalksOneLadderTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.definition = TaskDefinition.objects.create(
            name="Patch", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard",
                         "actions": [{"type": "restart_service",
                                      "params": {"service_name": "ssh"}}]})
        self.srv = _wave("srv-canary", 1, ["srv"], groups=["Servers"],
                         validation_hours=0)
        self.desk = _wave("desk-canary", 1, ["desk"], groups=["Desktops"],
                          validation_hours=0)
        _host("srv1", ["srv"])
        _host("desk1", ["desk"])

    def test_a_rollout_can_be_sent_to_just_one_group(self):
        rollout = start_rollout(self.definition, user=self.admin,
                                wave_group_tag="Servers")
        self.assertEqual(rollout.wave_group_tag, "Servers")
        self.assertEqual(rollout.current_wave_id, self.srv.id)

    def test_it_only_dispatches_to_that_group_s_machines(self):
        start_rollout(self.definition, user=self.admin, wave_group_tag="Servers")
        hostnames = set(Task.objects.values_list("host__hostname", flat=True))
        self.assertEqual(hostnames, {"srv1"})

    def test_the_group_tag_folds_case(self):
        rollout = start_rollout(self.definition, user=self.admin,
                                wave_group_tag="servers")
        self.assertEqual(rollout.current_wave_id, self.srv.id)

    def test_no_group_walks_every_enabled_wave(self):
        rollout = start_rollout(self.definition, user=self.admin)
        self.assertEqual(rollout.wave_group_tag, "")
        self.assertIn(rollout.current_wave_id, {self.srv.id, self.desk.id})

    def test_a_group_nobody_tagged_is_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            start_rollout(self.definition, user=self.admin,
                          wave_group_tag="Nonexistent")
        self.assertIn("Nonexistent", str(ctx.exception))

    def test_the_next_wave_stays_inside_the_group(self):
        second = _wave("srv-broad", 2, ["srv2"], groups=["Servers"],
                       validation_hours=0)
        _wave("desk-broad", 2, ["desk"], groups=["Desktops"],
              validation_hours=0)
        from apps.tasks.rollout import _next_enabled_wave
        self.assertEqual(_next_enabled_wave(1, "Servers").id, second.id)

    def test_the_rollout_reports_the_group_it_walks(self):
        rollout = start_rollout(self.definition, user=self.admin,
                                wave_group_tag="Servers")
        body = self.client.get(f"/api/v1/rollouts/{rollout.id}/").json()
        self.assertEqual(body["wave_group_tag"], "Servers")


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
        _wave("canary", 1, ["srv"], validation_hours=0)
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
