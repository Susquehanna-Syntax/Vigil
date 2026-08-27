"""Staged rollout engine tests — the advance/halt brain of phase 09.

Every test drives the real ``evaluate_rollout`` over real task rows; the
clock is patched, never slept.
"""

import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from apps.accounts.models import UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from apps.hosts.models import Host
from apps.tasks.models import (
    PatchRing,
    PatchRollout,
    Task,
    TaskDefinition,
    ring_host_ids,
    rollout_ring_plan,
)
from apps.tasks.rollout import (
    FAILURE_STATES,
    evaluate_rollout,
    halt_rollout,
    resume_rollout,
    start_rollout,
)
from apps.tasks.spec import parse_and_validate

PATCH_YAML = (
    "name: Patch OS\nrisk: low\nactions:\n"
    "  - type: run_command\n    params:\n"
    "      command: apt-get upgrade\n"
)


def make_definition(user, name="Patch OS"):
    return TaskDefinition.objects.create(
        owner=user,
        name=name,
        yaml_source=PATCH_YAML,
        parsed_spec=parse_and_validate(PATCH_YAML),
    )


def make_ring(name, order, tags, soak_hours=24, enabled=True):
    return PatchRing.objects.create(
        name=name, order=order, tags=tags,
        soak_hours=soak_hours, enabled=enabled,
    )


def make_host(hostname, tags, status=Host.Status.ONLINE):
    return Host.objects.create(
        hostname=hostname, agent_token=hostname + "tok0123456789",
        status=status, mode=Host.Mode.MANAGED, tags=tags,
    )


def set_task_states(tasks, states):
    """Set each task's state, stamping terminal fields like the agent would."""
    for task, state in zip(tasks, states):
        task.state = state
        if state in (Task.State.COMPLETED,) + FAILURE_STATES:
            task.completed_at = now()
            if state == Task.State.FAILED:
                task.error_message = "exit 1"
        task.save()


class RolloutStartTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.definition = make_definition(self.user)
        self.canary = make_ring("Canary", 1, ["patch:canary"], soak_hours=0)
        self.broad = make_ring("Broad", 2, ["role:web"], soak_hours=0)
        self.rest = make_ring("Rest of fleet", 3, ["role:db"], soak_hours=0)
        self.canary_host = make_host("canary-1", ["patch:canary"])
        self.web1 = make_host("web-1", ["role:web"])
        self.web2 = make_host("web-2", ["role:web"])
        self.db1 = make_host("db-1", ["role:db"])

    def test_only_first_ring_dispatches_initially(self):
        rollout = start_rollout(self.definition, user=self.user)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)
        self.assertEqual(rollout.current_ring_id, self.canary.id)
        self.assertIsNotNone(rollout.started_at)
        self.assertIsNotNone(rollout.ring_started_at)
        self.assertIsNone(rollout.finished_at)

        by_host = {t.host_id: t for t in Task.objects.filter(run__rollout=rollout)}
        self.assertEqual(set(by_host), {self.canary_host.id})
        # Ring 2 and 3 hosts must have no task yet.
        self.assertEqual(Task.objects.filter(run__rollout=rollout, host=self.web1).count(), 0)
        self.assertEqual(Task.objects.filter(run__rollout=rollout, host=self.db1).count(), 0)

    def test_start_without_any_ring_is_rejected(self):
        PatchRing.objects.all().delete()
        with self.assertRaisesRegex(ValueError, "no enabled patch rings"):
            start_rollout(self.definition, user=self.user)
        self.assertFalse(PatchRollout.objects.exists())

    def test_start_with_required_input_is_rejected(self):
        yaml_src = (
            "name: P\nrisk: low\ninputs:\n  - id: ver\n    type: text\n    required: true\n"
            "actions:\n  - type: run_command\n    params:\n        command: echo {{ inputs.ver }}\n"
        )
        bad = TaskDefinition.objects.create(
            owner=self.user, name="P", yaml_source=yaml_src,
            parsed_spec=parse_and_validate(yaml_src),
        )
        with self.assertRaisesRegex(ValueError, "cannot roll out"):
            start_rollout(bad, user=self.user)


class HostSelectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.definition = make_definition(self.user)

    def test_host_in_two_rings_is_patched_once(self):
        ring_a = make_ring("A", 1, ["patch:canary"], soak_hours=0)
        ring_b = make_ring("B", 2, ["role:web"], soak_hours=0)
        overlap = make_host("overlap", ["patch:canary", "role:web"])
        only_b = make_host("only-b", ["role:web"])

        plan = rollout_ring_plan(PatchRing.objects.all())
        self.assertEqual(plan[ring_a.id], [overlap.id])
        self.assertEqual(plan[ring_b.id], [only_b.id])

        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)),
            [Task.State.COMPLETED] * 1,
        )
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.current_ring_id, ring_b.id)
        # The overlapping host must have exactly one task in the whole rollout.
        self.assertEqual(
            Task.objects.filter(run__rollout=rollout, host=overlap).count(), 1
        )
        self.assertEqual(
            Task.objects.filter(run__rollout=rollout, host=only_b).count(), 1
        )

    def test_untagged_host_is_never_patched(self):
        make_ring("Canary", 1, ["patch:canary"], soak_hours=0)
        make_ring("Broad", 2, ["role:web"], soak_hours=0)
        stranger = make_host("stranger", ["os:linux"])
        make_host("canary-1", ["patch:canary"])
        web_host = make_host("web-1", ["role:web"])

        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)), [Task.State.COMPLETED]
        )
        evaluate_rollout(rollout)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout, host=web_host)),
            [Task.State.COMPLETED],
        )
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.COMPLETED)
        self.assertEqual(Task.objects.filter(run__rollout=rollout, host=stranger).count(), 0)

    def test_rejected_host_is_excluded(self):
        ring = make_ring("Canary", 1, ["patch:canary"], soak_hours=0)
        make_host("dead", ["patch:canary"], status=Host.Status.REJECTED)
        live = make_host("canary-1", ["patch:canary"])
        self.assertEqual(ring_host_ids(ring), [live.id])


class FailureGateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.definition = make_definition(self.user)

    def _ring_with_hosts(self, n, tag="patch:canary", soak_hours=0, threshold=10, minimum=3):
        ring = make_ring("Canary", 1, [tag], soak_hours=soak_hours)
        hosts = [make_host(f"h-{i}", [tag]) for i in range(n)]
        rollout = PatchRollout.objects.create(
            definition=self.definition,
            state=PatchRollout.State.RUNNING,
            current_ring=ring,
            failure_threshold_pct=threshold,
            min_results_before_halt=minimum,
            started_at=now(),
            ring_started_at=now(),
            created_by=self.user,
        )
        from apps.tasks.rollout import _dispatch_ring, _validate_definition

        spec = _validate_definition(self.definition)
        _dispatch_ring(rollout, spec)
        return rollout, hosts

    def _all_tasks(self, rollout):
        return list(Task.objects.filter(run__rollout=rollout).order_by("host__hostname"))

    def test_halt_requires_minimum_results(self):
        # 1-of-1 failure, threshold 10, min 3 -> NOT halted (too few results).
        rollout, (host,) = self._ring_with_hosts(1, minimum=3)
        set_task_states(self._all_tasks(rollout), [Task.State.FAILED])
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertNotEqual(rollout.state, PatchRollout.State.HALTED)

    def test_halts_above_threshold(self):
        # 2-of-10 at threshold 10 -> 20% > 10% -> halted.
        rollout, hosts = self._ring_with_hosts(10)
        tasks = self._all_tasks(rollout)
        set_task_states(tasks, [Task.State.FAILED] * 2 + [Task.State.COMPLETED] * 8)
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.HALTED)
        self.assertIn("Canary", rollout.halted_reason)
        self.assertIn("20.0%", rollout.halted_reason)
        self.assertIn("2/10", rollout.halted_reason)

    def test_exactly_at_threshold_proceeds(self):
        # 1-of-10 at threshold 10 -> exactly 10% -> NOT strictly greater -> proceeds.
        ring2 = make_ring("Broad", 2, ["role:web"], soak_hours=0)
        make_host("web-1", ["role:web"])
        rollout, hosts = self._ring_with_hosts(10)
        tasks = self._all_tasks(rollout)
        set_task_states(tasks, [Task.State.FAILED] + [Task.State.COMPLETED] * 9)
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)
        self.assertEqual(rollout.current_ring_id, ring2.id)
        self.assertEqual(
            Task.objects.filter(run__rollout=rollout, host=hosts[0]).count(), 1
        )

    def test_expires_count_as_failures(self):
        # EXPIRED and REJECTED are failure states for the gate.
        rollout, hosts = self._ring_with_hosts(3)
        tasks = self._all_tasks(rollout)
        set_task_states(tasks, [Task.State.EXPIRED, Task.State.REJECTED, Task.State.COMPLETED])
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.HALTED)
        self.assertIn("2/3", rollout.halted_reason)


class SoakAndCompletionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.definition = make_definition(self.user)

    def _two_rings(self, soak_hours=24):
        ring1 = make_ring("Canary", 1, ["patch:canary"], soak_hours=soak_hours)
        ring2 = make_ring("Broad", 2, ["role:web"], soak_hours=0)
        self.canary_host = make_host("canary-1", ["patch:canary"])
        self.web_host = make_host("web-1", ["role:web"])
        return ring1, ring2

    def test_soak_blocks_advance_until_elapsed(self):
        ring1, ring2 = self._two_rings(soak_hours=24)
        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)), [Task.State.COMPLETED]
        )
        # Ring passed, soak window not over -> soaking, ring 2 NOT dispatched.
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.SOAKING)
        self.assertEqual(rollout.current_ring_id, ring1.id)
        self.assertEqual(Task.objects.filter(run__rollout=rollout, host=self.web_host).count(), 0)

        # Patch the clock 25h forward -> soaked -> advances to ring 2.
        frozen = now() + timedelta(hours=25)
        with patch("apps.tasks.rollout._now", return_value=frozen):
            evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)
        self.assertEqual(rollout.current_ring_id, ring2.id)
        self.assertEqual(
            Task.objects.filter(run__rollout=rollout, host=self.web_host).count(), 1
        )

    def test_soak_not_elapsed_yet_stays_soaking(self):
        ring1, _ = self._two_rings(soak_hours=24)
        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)), [Task.State.COMPLETED]
        )
        frozen = now() + timedelta(hours=1)
        with patch("apps.tasks.rollout._now", return_value=frozen):
            evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.SOAKING)
        self.assertEqual(rollout.current_ring_id, ring1.id)

    def test_completed_when_no_rings_remain(self):
        self._two_rings(soak_hours=0)
        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)), [Task.State.COMPLETED]
        )
        evaluate_rollout(rollout)
        set_task_states(
            list(Task.objects.filter(
                run__rollout=rollout, host=self.web_host
            )), [Task.State.COMPLETED]
        )
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.COMPLETED)
        self.assertIsNotNone(rollout.finished_at)
        self.assertEqual(Task.objects.filter(run__rollout=rollout, host=self.web_host).count(), 1)

    def test_disabled_ring_is_skipped(self):
        ring1 = make_ring("Canary", 1, ["patch:canary"], soak_hours=0)
        make_ring("Disabled", 2, ["role:db"], soak_hours=0, enabled=False)
        ring3 = make_ring("Broad", 3, ["role:web"], soak_hours=0)
        make_host("canary-1", ["patch:canary"])
        make_host("db-1", ["role:db"])
        make_host("web-1", ["role:web"])

        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)), [Task.State.COMPLETED]
        )
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        # Must land on ring 3, skipping the disabled ring 2.
        self.assertEqual(rollout.current_ring_id, ring3.id)
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)

    def test_no_next_enabled_ring_completes(self):
        # Last enabled ring passes with nothing after it.
        ring1 = make_ring("Only", 1, ["patch:canary"], soak_hours=0)
        make_ring("Disabled", 2, ["role:db"], soak_hours=0, enabled=False)
        make_host("canary-1", ["patch:canary"])
        make_host("db-1", ["role:db"])
        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)), [Task.State.COMPLETED]
        )
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.COMPLETED)
        self.assertIsNotNone(rollout.finished_at)


class HaltResumeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.definition = make_definition(self.user)
        self.ring = make_ring("Canary", 1, ["patch:canary"], soak_hours=0)
        self.host = make_host("canary-1", ["patch:canary"])
        self.rollout = start_rollout(self.definition, user=self.user)

    def test_halted_rollout_does_not_advance(self):
        set_task_states(
            list(Task.objects.filter(run__rollout=self.rollout)), [Task.State.FAILED]
        )
        halt_rollout(self.rollout, user=self.user, reason="operator stop")
        self.rollout.refresh_from_db()
        self.assertEqual(self.rollout.state, PatchRollout.State.HALTED)
        self.assertEqual(self.rollout.halted_reason, "operator stop")
        self.assertEqual(self.rollout.halted_by, self.user)

        # A beat tick must not move a halted rollout.
        evaluate_rollout(self.rollout)
        self.rollout.refresh_from_db()
        self.assertEqual(self.rollout.state, PatchRollout.State.HALTED)
        self.assertIsNone(self.rollout.finished_at)

    def test_halt_completed_is_rejected(self):
        self.rollout.state = PatchRollout.State.COMPLETED
        self.rollout.finished_at = now()
        self.rollout.save()
        with self.assertRaisesRegex(ValueError, "not active"):
            halt_rollout(self.rollout, user=self.user)

    def test_resume_requeues_failed_tasks_and_records_who(self):
        set_task_states(
            list(Task.objects.filter(run__rollout=self.rollout)), [Task.State.FAILED]
        )
        halt_rollout(self.rollout, user=self.user, reason="bad patch")
        resume_rollout(self.rollout, user=self.user)
        self.rollout.refresh_from_db()
        self.assertEqual(self.rollout.state, PatchRollout.State.RUNNING)
        self.assertEqual(self.rollout.halted_reason, "")
        self.assertEqual(self.rollout.resumed_by, self.user)
        task = Task.objects.get(run__rollout=self.rollout)
        self.assertEqual(task.state, Task.State.PENDING)
        # The nonce must be fresh so the agent treats it as a new dispatch.
        self.assertNotEqual(task.nonce, "0" * 64)

    def test_resume_non_halted_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not halted"):
            resume_rollout(self.rollout, user=self.user)


class ConcurrencyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="pw")
        self.definition = make_definition(self.user)
        self.ring1 = make_ring("Canary", 1, ["patch:canary"], soak_hours=0)
        self.ring2 = make_ring("Broad", 2, ["role:web"], soak_hours=0)
        self.canary_host = make_host("canary-1", ["patch:canary"])
        self.web_host = make_host("web-1", ["role:web"])

    def test_concurrent_evaluation_dispatches_once(self):
        rollout = start_rollout(self.definition, user=self.user)
        set_task_states(
            list(Task.objects.filter(run__rollout=rollout)), [Task.State.COMPLETED]
        )
        # Two beat ticks "at the same time": both see the ring as passed and
        # both try to advance. The row lock must keep dispatch to one ring run.
        evaluate_rollout(rollout)
        evaluate_rollout(rollout)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)
        self.assertEqual(rollout.current_ring_id, self.ring2.id)
        # Exactly one task for the ring-2 host — not two.
        self.assertEqual(
            Task.objects.filter(run__rollout=rollout, host=self.web_host).count(), 1
        )
        self.assertEqual(rollout.runs.count(), 2)


class RolloutApiTests(TestCase):
    """The rollout endpoints follow the deploy pattern: TOTP for the
    operator, admin on top of that for the halt (the heavier decision)."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.operator = get_user_model().objects.create_user("op", password="pw")
        self.secret = generate_secret()
        profile = UserProfile.objects.create(user=self.operator)
        profile.totp_secret = self.secret
        profile.totp_confirmed_at = now()
        profile.save()
        self.client.force_authenticate(self.operator)

        self.definition = make_definition(self.operator)
        make_ring("Canary", 1, ["patch:canary"], soak_hours=0)
        make_ring("Broad", 2, ["role:web"], soak_hours=0)
        make_host("canary-1", ["patch:canary"])
        make_host("web-1", ["role:web"])

    def _totp(self):
        return generate_totp(self.secret)

    def test_api_start_requires_totp(self):
        resp = self.client.post(
            "/api/v1/rollouts/",
            {"definition_id": str(self.definition.id)}, format="json",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(PatchRollout.objects.exists())

    def test_api_start_creates_rollout_and_dispatches_ring1(self):
        resp = self.client.post(
            "/api/v1/rollouts/",
            {"definition_id": str(self.definition.id), "totp": self._totp()},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, getattr(resp, "data", None))
        rollout = PatchRollout.objects.get()
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)
        self.assertEqual(rollout.created_by, self.operator)
        # Ring 1 only, with its ring progress in the payload.
        rings = {r["name"]: r for r in resp.data["rings"]}
        self.assertEqual(rings["Canary"]["status"], "running")
        self.assertEqual(rings["Broad"]["status"], "pending")
        self.assertEqual(rings["Canary"]["tasks_total"], 1)

    def test_api_start_without_definition_is_rejected(self):
        resp = self.client.post(
            "/api/v1/rollouts/", {"totp": self._totp()}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_api_list_filters_by_state(self):
        rollout = start_rollout(self.definition, user=self.operator)
        halt_rollout(rollout, user=self.operator, reason="stop")
        resp = self.client.get("/api/v1/rollouts/?state=halted")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["id"], str(rollout.id))
        self.assertEqual(resp.data[0]["halted_reason"], "stop")
        # Unknown state -> 400, not an empty list.
        bad = self.client.get("/api/v1/rollouts/?state=bogus")
        self.assertEqual(bad.status_code, 400)

    def test_api_detail_404_for_unknown_rollout(self):
        resp = self.client.get("/api/v1/rollouts/00000000-0000-4000-8000-000000000000/")
        self.assertEqual(resp.status_code, 404)

    def test_api_halt_requires_admin(self):
        rollout = start_rollout(self.definition, user=self.operator)
        resp = self.client.post(
            f"/api/v1/rollouts/{rollout.id}/halt/",
            {"totp": self._totp()}, format="json",
        )
        self.assertEqual(resp.status_code, 403)
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)

    def test_api_halt_and_resume_by_admin(self):
        from apps.accounts.totp import generate_secret as gs, generate_totp as gt

        rollout = start_rollout(self.definition, user=self.operator)
        admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True,
        )
        a_secret = gs()
        from apps.accounts.models import UserProfile

        prof = UserProfile.objects.create(user=admin)
        prof.totp_secret = a_secret
        prof.totp_confirmed_at = now()
        prof.save()
        self.client.force_authenticate(admin)

        # Two codes from different TOTP steps: the replay guard burns a
        # code for 90s, so one code cannot serve both calls.
        t0 = time.time()
        halt_code = gt(a_secret, at=t0 - 30)
        resume_code = gt(a_secret, at=t0 + 30)

        resp = self.client.post(
            f"/api/v1/rollouts/{rollout.id}/halt/",
            {"reason": "canary failing", "totp": halt_code}, format="json",
        )
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", None))
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.HALTED)
        self.assertEqual(rollout.halted_reason, "canary failing")
        self.assertEqual(rollout.halted_by, admin)

        resp = self.client.post(
            f"/api/v1/rollouts/{rollout.id}/resume/",
            {"totp": resume_code}, format="json",
        )
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", None))
        rollout.refresh_from_db()
        self.assertEqual(rollout.state, PatchRollout.State.RUNNING)
        self.assertEqual(rollout.resumed_by, admin)
        self.assertEqual(rollout.halted_reason, "")
