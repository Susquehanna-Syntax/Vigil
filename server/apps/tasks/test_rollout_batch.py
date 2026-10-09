"""Parallel wave groups: one request, one rollout per ladder, one batch id.

Starting "Servers" and "Desktops" side by side used to mean two rollouts and
two TOTP codes, and a host tagged into both ladders got the task twice. A
batch is one request that starts them together and hands every host to exactly
one of them.
"""

import time
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from apps.hosts.models import Host
from apps.tasks import rollout as rollout_module
from apps.tasks.models import PatchRollout, PatchWave, Task, TaskDefinition
from apps.tasks.rollout import halt_batch, start_rollout_batch
from apps.tasks.spec import parse_and_validate

PATCH_YAML = (
    "name: Patch OS\nrisk: low\nactions:\n"
    "  - type: run_command\n    params:\n"
    "      command: apt-get upgrade\n"
)


def _definition(owner=None, name="Patch OS"):
    return TaskDefinition.objects.create(
        owner=owner, name=name, yaml_source=PATCH_YAML,
        parsed_spec=parse_and_validate(PATCH_YAML),
    )


def _host(name, tags):
    return Host.objects.create(
        hostname=name, ip_address=f"10.70.0.{abs(hash(name)) % 250 + 1}",
        agent_token=f"t-{name}", tags=tags, status=Host.Status.ONLINE,
        mode="managed")


def _wave(name, order, tags, groups=(), **kw):
    return PatchWave.objects.create(
        name=name, order=order, tags=tags, group_tags=list(groups), **kw)


_real_host_plan = rollout_module._batch_host_plan


def _batch(case, tags, host_ids=None):
    """``start_rollout_batch``, reporting the host partition computed for
    **every** named group in tag order — an emptied group included, which is
    what a ladder whose every host went to an earlier group looks like.

    The real call skips a group left without machines before starting it, so a
    batch's rollouts alone cannot show which machines it withheld from a later
    group: ``["Servers"]`` looks the same whether Desktops was emptied by the
    split or simply never matched a host. The partition is what the split
    returns, so this reads it where it is decided.

    Returns ``(started_tags, hosts_by_tag)``.
    """
    def keep_emptied_groups(tags_in, host_ids_in):
        per_tag = _real_host_plan(tags_in, host_ids_in)
        for tag in tags_in:
            hosts_by_tag[tag] = [str(h) for h in per_tag.get(tag, [])]
            per_tag[tag] = hosts_by_tag[tag]
        return per_tag

    hosts_by_tag: dict[str, list[str]] = {}
    with patch.object(rollout_module, "_batch_host_plan", keep_emptied_groups):
        rollouts = rollout_module.start_rollout_batch(
            case.definition, user=case.user, group_tags=tags,
            host_ids=host_ids)
    assert rollouts
    return [r.wave_group_tag for r in rollouts], hosts_by_tag


class BatchStartTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.definition = _definition(self.user)

    def test_each_group_gets_its_own_rollout_in_one_batch(self):
        _wave("srv-1", 1, ["srv1"], groups=["Servers"], validation_hours=0)
        _wave("srv-2", 2, ["srv2"], groups=["Servers"], validation_hours=0)
        _wave("desk-1", 1, ["desk"], groups=["Desktops"], validation_hours=0)
        _host("srv-a", ["srv1"])
        _host("srv-b", ["srv2"])
        _host("desk-a", ["desk"])
        rollouts = start_rollout_batch(
            self.definition, user=self.user, group_tags=["Servers", "Desktops"])
        self.assertEqual([r.wave_group_tag for r in rollouts],
                         ["Servers", "Desktops"])
        self.assertEqual(len({r.batch for r in rollouts}), 1)
        self.assertIsNotNone(rollouts[0].batch)
        for r in rollouts:
            self.assertEqual(r.state, PatchRollout.State.RUNNING)

    def test_a_host_on_two_ladders_goes_to_the_first_listed_group(self):
        # One Canary wave is the first rung of **both** ladders (the case the
        # model's group_tags note describes), so the fleet-wide plan hands its
        # machine to that one wave — and both group rollouts walk it. Only the
        # batch's split stops the machine being patched twice.
        canary = _host("canary-1", ["canary"])
        srv = _host("srv-a", ["srv"])
        desk = _host("desk-a", ["desk"])
        _wave("canary", 1, ["canary"], groups=["Servers", "Desktops"], validation_hours=0)
        _wave("srv-2", 2, ["srv"], groups=["Servers"], validation_hours=0)
        _wave("desk-2", 2, ["desk"], groups=["Desktops"], validation_hours=0)
        started, hosts = _batch(self, ["Desktops", "Servers"])
        self.assertEqual(started, ["Desktops", "Servers"])
        self.assertEqual(hosts["Desktops"], [str(canary.id), str(desk.id)])
        self.assertEqual(hosts["Servers"], [str(srv.id)])
        self.assertEqual(Task.objects.filter(host=canary).count(), 1)
    def test_unknown_group_refuses_the_whole_batch(self):
        _wave("srv-1", 1, ["srv1"], groups=["Servers"], validation_hours=0)
        _host("srv-a", ["srv1"])
        with self.assertRaises(ValueError) as ctx:
            start_rollout_batch(self.definition, user=self.user,
                                group_tags=["Servers", "Nope"])
        self.assertIn("Nope", str(ctx.exception))
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_a_group_with_no_machines_left_is_skipped(self):
        # desk-canary is the only rung the Desktops ladder has, and the one
        # machine matching it goes to Servers, which is listed first. Desktops
        # is left with no machine at all — no rollout for it, nothing dispatched
        # from it — and the batch is Servers walking one ladder.
        _host("canary-1", ["canary"])
        _wave("srv-canary", 1, ["canary"], groups=["Servers"],
              validation_hours=0)
        _wave("desk-canary", 1, ["canary"], groups=["Desktops"],
              validation_hours=0)
        started, hosts = _batch(self, ["Servers", "Desktops"])
        self.assertEqual(started, ["Servers"])
        self.assertEqual(hosts["Servers"], [str(Host.objects.get(hostname="canary-1").id)])
        self.assertEqual(hosts["Desktops"], [])
        self.assertEqual(PatchRollout.objects.count(), 1)
        self.assertEqual(Task.objects.count(), 1)

    def test_no_group_left_with_a_machine_is_refused(self):
        # One machine sits on both ladders, and the request narrowed to a host
        # nobody has: no group is left with anything to send this to.
        _host("dual-1", ["dual"])
        _wave("dual", 1, ["dual"], groups=["Servers", "Desktops"],
              validation_hours=0)
        with self.assertRaises(ValueError) as ctx:
            start_rollout_batch(self.definition, user=self.user,
                                group_tags=["Servers", "Desktops"],
                                host_ids=[str(uuid.uuid4())])
        self.assertIn("none of those groups", str(ctx.exception))
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_fewer_than_two_groups_is_refused(self):
        _wave("srv-1", 1, ["srv1"], groups=["Servers"], validation_hours=0)
        _host("srv-a", ["srv1"])
        with self.assertRaises(ValueError) as ctx:
            start_rollout_batch(self.definition, user=self.user,
                                group_tags=["Servers"])
        self.assertIn("at least two", str(ctx.exception))
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_the_same_group_listed_twice_counts_once(self):
        _wave("srv-1", 1, ["srv1"], groups=["Servers"], validation_hours=0)
        _wave("desk-1", 1, ["desk"], groups=["Desktops"], validation_hours=0)
        _host("srv-a", ["srv1"])
        _host("desk-a", ["desk"])
        with self.assertRaises(ValueError):
            start_rollout_batch(self.definition, user=self.user,
                                group_tags=["Servers", " servers ", "Servers"])

    def test_the_batch_halt_stops_every_rollout(self):
        _wave("srv-1", 1, ["srv1"], groups=["Servers"], validation_hours=0)
        _wave("desk-1", 1, ["desk"], groups=["Desktops"], validation_hours=0)
        _host("srv-a", ["srv1"])
        _host("desk-a", ["desk"])
        rollouts = start_rollout_batch(
            self.definition, user=self.user, group_tags=["Servers", "Desktops"])
        self.assertEqual(halt_batch(rollouts[0].batch, user=self.user,
                                    reason="stop"), 2)
        for r in rollouts:
            r.refresh_from_db()
            self.assertEqual(r.state, PatchRollout.State.HALTED)
        with self.assertRaises(ValueError):
            halt_batch(rollouts[0].batch, user=self.user)


class BatchApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.operator = get_user_model().objects.create_user("op", password="pw")
        self.secret = generate_secret()
        profile = UserProfile.objects.create(user=self.operator, role=Role.OPERATOR)
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()
        self.client.force_authenticate(self.operator)

        self.definition = _definition(self.operator)
        _wave("srv-canary", 1, ["srv1"], groups=["Servers"], validation_hours=0)
        _wave("desk-canary", 1, ["desk"], groups=["Desktops"], validation_hours=0)
        _host("srv-1", ["srv1"])
        _host("desk-1", ["desk"])

    def _totp(self):
        return generate_totp(self.secret)

    def _post_batch(self, tags, **extra):
        return self.client.post(
            "/api/v1/rollouts/",
            {"definition_id": str(self.definition.id),
             "wave_group_tags": tags, "totp": self._totp(), **extra},
            format="json",
        )

    def test_api_starts_a_batch(self):
        resp = self._post_batch(["Servers", "Desktops"])
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))
        body = resp.json()
        self.assertEqual(len(body["rollouts"]), 2)
        self.assertEqual({r["batch"] for r in body["rollouts"]},
                         {body["batch"]})

    def test_api_refuses_a_batch_without_totp(self):
        resp = self.client.post(
            "/api/v1/rollouts/",
            {"definition_id": str(self.definition.id),
             "wave_group_tags": ["Servers", "Desktops"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_api_refuses_a_viewer(self):
        viewer = get_user_model().objects.create_user("view", password="pw")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        self.client.force_authenticate(viewer)
        resp = self.client.post(
            "/api/v1/rollouts/",
            {"definition_id": str(self.definition.id),
             "wave_group_tags": ["Servers", "Desktops"], "totp": "123456"},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_api_refuses_wave_group_tags_that_are_not_a_list_of_strings(self):
        resp = self._post_batch("Servers")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_api_refuses_both_group_fields_at_once(self):
        resp = self._post_batch(["Servers", "Desktops"], wave_group_tag="Servers")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_single_group_start_is_unchanged(self):
        resp = self.client.post(
            "/api/v1/rollouts/",
            {"definition_id": str(self.definition.id),
             "wave_group_tag": "Servers", "totp": self._totp()},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))
        body = resp.json()
        self.assertIn("id", body)
        self.assertNotIn("rollouts", body)
        self.assertIsNone(body["batch"])

    def test_batch_halt_halts_every_running_rollout_with_one_code(self):
        resp = self._post_batch(["Servers", "Desktops"])
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))
        batch = resp.json()["batch"]

        admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        a_secret = generate_secret()
        prof = UserProfile.objects.create(user=admin)
        prof.totp_secret = a_secret
        prof.totp_confirmed_at = now()
        prof.save()
        self.client.force_authenticate(admin)

        # The replay guard burns a code for its whole window, so the halt uses
        # a code from a different step than the one that started the batch.
        code = generate_totp(a_secret, at=time.time() + 30)
        halted = self.client.post(
            f"/api/v1/rollouts/batch/{batch}/halt/",
            {"reason": "recall", "totp": code}, format="json",
        )
        self.assertEqual(halted.status_code, 200, getattr(halted, "data", None))
        self.assertEqual(halted.json(), {"halted": 2})
        for rollout in PatchRollout.objects.filter(batch=batch):
            self.assertEqual(rollout.state, PatchRollout.State.HALTED)
            self.assertEqual(rollout.halted_reason, "recall")

    def test_batch_halt_needs_admin(self):
        resp = self._post_batch(["Servers", "Desktops"])
        batch = resp.json()["batch"]
        denied = self.client.post(
            f"/api/v1/rollouts/batch/{batch}/halt/",
            {"totp": generate_totp(self.secret, at=time.time() + 30)},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

    def test_batch_halt_404_for_an_unknown_batch(self):
        admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        a_secret = generate_secret()
        prof = UserProfile.objects.create(user=admin)
        prof.totp_secret = a_secret
        prof.totp_confirmed_at = now()
        prof.save()
        self.client.force_authenticate(admin)
        resp = self.client.post(
            f"/api/v1/rollouts/batch/{uuid.uuid4()}/halt/",
            {"totp": generate_totp(a_secret)}, format="json",
        )
        self.assertEqual(resp.status_code, 404)
