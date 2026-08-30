"""Rolling out a baseline, and automations that dispatch wave by wave.

A rollout can carry either a task definition or a baseline. The baseline case
reuses the existing `type: baseline` composition rather than a second expansion
path, so these tests mostly prove the plumbing picks the right target and keeps
picking it on later waves — which is where a `rollout.definition` assumption
would break rather than at start.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.automations.models import Automation
from apps.baselines.models import Baseline, BaselineStep
from apps.hosts.models import Host

from .models import PatchRollout, PatchWave, TaskDefinition
from .rollout import rollout_spec, start_rollout
from .spec import parse_and_validate

YAML = ("name: Patch\nrisk: low\nactions:\n"
        "  - type: clear_temp_files\n    params:\n      older_than_days: 7\n")


class RolloutBaselineTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("ro", password="x",
                                             is_staff=True, is_superuser=True)
        PatchWave.objects.create(name="Canary", order=1, tags=["canary"],
                                 validation_hours=0)
        Host.objects.create(hostname="c1", ip_address="10.9.0.1",
                            tags=["canary"], agent_token="tok-c1")
        self.definition = TaskDefinition.objects.create(
            name="Clear temp", owner=self.user, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")
        self.baseline = Baseline.objects.create(name="Nightly tidy",
                                                created_by=self.user)
        BaselineStep.objects.create(baseline=self.baseline,
                                    definition=self.definition, order=1)

    def test_rollout_from_a_definition_still_works(self):
        r = start_rollout(self.definition, user=self.user)
        self.assertEqual(r.action_kind, PatchRollout.ActionKind.TASK)
        self.assertEqual(r.target_name, "Clear temp")

    def test_rollout_from_a_baseline(self):
        r = start_rollout(baseline=self.baseline, user=self.user)
        self.assertEqual(r.action_kind, PatchRollout.ActionKind.BASELINE)
        self.assertIsNone(r.definition)
        self.assertEqual(r.target_name, "Nightly tidy")

    def test_baseline_rollout_dispatches_the_baselines_steps(self):
        r = start_rollout(baseline=self.baseline, user=self.user)
        run = r.runs.first()
        self.assertIsNotNone(run)
        self.assertEqual(run.host_count, 1)
        self.assertEqual(run.name_snapshot, "Nightly tidy")

    def test_spec_is_re_derivable_for_later_waves(self):
        """The advance path re-derives the spec. A baseline rollout has no
        definition, so anything reaching for it breaks on wave 2, not wave 1."""
        r = start_rollout(baseline=self.baseline, user=self.user)
        spec = rollout_spec(r)
        self.assertEqual(spec["actions"][0]["type"], "baseline")
        self.assertEqual(spec["actions"][0]["params"]["name"], "Nightly tidy")

    def test_both_targets_is_rejected(self):
        with self.assertRaises(ValueError):
            start_rollout(self.definition, baseline=self.baseline, user=self.user)

    def test_neither_target_is_rejected(self):
        with self.assertRaises(ValueError):
            start_rollout(user=self.user)

    def test_disabled_baseline_is_refused(self):
        self.baseline.enabled = False
        self.baseline.save(update_fields=["enabled"])
        with self.assertRaises(ValueError):
            start_rollout(baseline=self.baseline, user=self.user)

    def test_deleting_the_baseline_cascades_to_its_rollouts(self):
        """Matches the pre-existing behaviour of the definition FK: deleting the
        thing a rollout ran removes the rollout with it. Worth knowing, because
        it means deleting a baseline also discards its rollout history."""
        r = start_rollout(baseline=self.baseline, user=self.user)
        self.baseline.delete()
        self.assertFalse(PatchRollout.objects.filter(pk=r.pk).exists())

    def test_database_refuses_a_rollout_with_no_target(self):
        """The CHECK constraint makes a targetless rollout unrepresentable.

        `rollout_spec` still guards against it defensively, but this is why that
        guard is unreachable in practice — the row cannot exist.
        """
        from django.db.utils import IntegrityError
        r = start_rollout(baseline=self.baseline, user=self.user)
        with self.assertRaises(IntegrityError):
            PatchRollout.objects.filter(pk=r.pk).update(baseline=None)

    def test_database_refuses_a_rollout_with_two_targets(self):
        from django.db.utils import IntegrityError
        r = start_rollout(baseline=self.baseline, user=self.user)
        with self.assertRaises(IntegrityError):
            PatchRollout.objects.filter(pk=r.pk).update(definition=self.definition)


class RolloutApiTargetTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("api", password="x",
                                             is_staff=True, is_superuser=True)
        PatchWave.objects.create(name="Canary", order=1, tags=["canary"])
        self.definition = TaskDefinition.objects.create(
            name="D", owner=self.user, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")
        self.baseline = Baseline.objects.create(name="B", created_by=self.user)
        BaselineStep.objects.create(baseline=self.baseline,
                                    definition=self.definition, order=1)
        self.client.force_login(self.user)

    def test_supplying_both_targets_is_a_400(self):
        r = self.client.post("/api/v1/rollouts/",
                             {"definition_id": str(self.definition.id),
                              "baseline_id": str(self.baseline.id)},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("exactly one", r.json()["detail"])

    def test_supplying_neither_is_a_400(self):
        r = self.client.post("/api/v1/rollouts/", {},
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)


class AutomationRolloutDispatchTests(TestCase):
    """An automation can hand its work to the waves instead of firing it at
    every matching host at once."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("auto", password="x")
        PatchWave.objects.create(name="Canary", order=1, tags=["canary"],
                                 validation_hours=0)
        Host.objects.create(hostname="c2", ip_address="10.9.0.2",
                            tags=["canary"], agent_token="tok-c2")
        self.definition = TaskDefinition.objects.create(
            name="D2", owner=self.user, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")

    def _automation(self, **kw):
        return Automation.objects.create(
            name="A", trigger=Automation.Trigger.EVENT, event="alert_fired",
            action_kind=Automation.ActionKind.TASK,
            task_definition=self.definition, created_by=self.user, **kw)

    def test_default_dispatch_mode_is_direct(self):
        self.assertEqual(self._automation().dispatch_mode,
                         Automation.DispatchMode.DIRECT)

    def test_rollout_mode_starts_a_rollout(self):
        from apps.automations.engine import _start_rollout_for
        a = self._automation(dispatch_mode=Automation.DispatchMode.ROLLOUT)
        self.assertEqual(_start_rollout_for(a), 1)
        self.assertEqual(PatchRollout.objects.count(), 1)
        self.assertEqual(PatchRollout.objects.first().definition, self.definition)

    def test_rollout_mode_with_no_waves_does_not_raise(self):
        """An automation that cannot roll out must not take the event bus down."""
        from apps.automations.engine import _start_rollout_for
        PatchWave.objects.all().delete()
        a = self._automation(dispatch_mode=Automation.DispatchMode.ROLLOUT)
        self.assertEqual(_start_rollout_for(a), 0)
        self.assertEqual(PatchRollout.objects.count(), 0)

    def test_rollout_mode_with_a_deleted_definition_does_not_raise(self):
        from apps.automations.engine import _start_rollout_for
        a = self._automation(dispatch_mode=Automation.DispatchMode.ROLLOUT)
        Automation.objects.filter(pk=a.pk).update(task_definition=None)
        a.refresh_from_db()
        self.assertEqual(_start_rollout_for(a), 0)


class SkipValidationTests(TestCase):
    """Ending a validation window early — an operator judgement call, but the
    failure gate still has the final say."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("skip", password="x",
                                             is_staff=True, is_superuser=True)
        self.w1 = PatchWave.objects.create(name="Canary", order=1,
                                           tags=["canary"], validation_hours=48)
        self.w2 = PatchWave.objects.create(name="Broad", order=2,
                                           tags=["broad"], validation_hours=0)
        Host.objects.create(hostname="s1", ip_address="10.8.0.1",
                            tags=["canary"], agent_token="tok-s1")
        Host.objects.create(hostname="s2", ip_address="10.8.0.2",
                            tags=["broad"], agent_token="tok-s2")
        self.definition = TaskDefinition.objects.create(
            name="S", owner=self.user, yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML), risk_level="low")

    def _validating_rollout(self):
        from apps.tasks.models import Task
        from apps.tasks.rollout import evaluate_rollout
        r = start_rollout(self.definition, user=self.user)
        # Report the canary wave's tasks as done so the gate passes and the
        # rollout parks in its 48h validation window.
        Task.objects.filter(run__rollout=r).update(state=Task.State.COMPLETED)
        evaluate_rollout(r)
        r.refresh_from_db()
        return r

    def test_rollout_parks_in_validation(self):
        r = self._validating_rollout()
        self.assertEqual(r.state, PatchRollout.State.VALIDATING)
        self.assertEqual(r.current_wave, self.w1)

    def test_skip_advances_to_the_next_wave(self):
        from apps.tasks.rollout import skip_validation
        r = self._validating_rollout()
        skip_validation(r, user=self.user)
        r.refresh_from_db()
        self.assertEqual(r.current_wave, self.w2)

    def test_skip_is_refused_when_not_validating(self):
        from apps.tasks.rollout import skip_validation
        r = start_rollout(self.definition, user=self.user)
        self.assertEqual(r.state, PatchRollout.State.RUNNING)
        with self.assertRaises(ValueError):
            skip_validation(r, user=self.user)

    def test_endpoint_requires_admin(self):
        r = self._validating_rollout()
        viewer = get_user_model().objects.create_user("v2", password="x")
        self.client.force_login(viewer)
        resp = self.client.post(f"/api/v1/rollouts/{r.id}/skip-validation/", {},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_endpoint_requires_totp(self):
        r = self._validating_rollout()
        self.client.force_login(self.user)
        resp = self.client.post(f"/api/v1/rollouts/{r.id}/skip-validation/", {},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 401)
