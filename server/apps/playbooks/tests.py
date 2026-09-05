import json
import uuid

from django.contrib.auth import get_user_model
from unittest.mock import patch

from django.test import TestCase

from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition
from vigil import hooks

from .apps import wire
from .expansion import PlaybookExpandError, expand_actions
from .models import Playbook, PlaybookStep, dispatch_to_host, eligible


def make_definition(*, risk="standard", actions=None, name=None):
    return TaskDefinition.objects.create(
        name=name or f"def-{uuid.uuid4().hex[:6]}",
        risk_level=risk,
        yaml_source="",
        parsed_spec={
            "risk": risk,
            "actions": actions if actions is not None else [
                {"type": "pkg_update", "params": {}},
            ],
        },
    )


def make_playbook(admin, *, name=None, definitions=None, tags=None, auto_enroll=True):
    b = Playbook.objects.create(
        name=name or f"bl-{uuid.uuid4().hex[:6]}",
        created_by=admin, target_tags=tags or [], auto_enroll=auto_enroll)
    for i, d in enumerate(definitions or [make_definition()]):
        PlaybookStep.objects.create(playbook=b, definition=d, order=i)
    return b


def make_host(mode=Host.Mode.MANAGED, tags=None):
    return Host.objects.create(
        hostname=f"h-{uuid.uuid4().hex[:6]}", mode=mode,
        status=Host.Status.ONLINE, tags=tags or [],
        agent_token=uuid.uuid4().hex,
    )


class PlaybookDispatchTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)

    def test_approved_host_gets_full_sequence_in_order(self):
        d1 = make_definition(actions=[{"type": "pkg_update", "params": {}}])
        d2 = make_definition(actions=[{"type": "restart_service",
                                       "params": {"service_name": "nginx"}}])
        make_playbook(self.admin, name="Linux bootstrap", definitions=[d1, d2])
        host = make_host()
        wire()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        task = Task.objects.get(host=host)
        self.assertEqual(task.step_label, "playbook: Linux bootstrap")
        self.assertEqual([s["action"] for s in task.params["steps"]],
                         ["pkg_update", "restart_service"])
        self.assertEqual(task.params["steps"][0]["id"], "step1")
        self.assertEqual(task.params["steps"][1]["id"], "step2")

    def test_monitor_mode_hosts_are_skipped(self):
        make_playbook(self.admin)
        host = make_host(mode=Host.Mode.MONITOR)
        wire()
        hooks.emit("host_approved", host=host, approved_by=self.admin)
        self.assertFalse(Task.objects.filter(host=host).exists())

    def test_tag_filter(self):
        b = make_playbook(self.admin, tags=["os:linux"])
        linux = make_host(tags=["os:linux"])
        windows = make_host(tags=["os:windows"])
        self.assertEqual(dispatch_to_host(linux, playbooks=[b]), 1)
        self.assertEqual(dispatch_to_host(windows, playbooks=[b]), 0)

    def test_high_risk_and_update_agent_are_ineligible(self):
        self.assertFalse(eligible(make_definition(risk="high"))[0])
        self.assertFalse(eligible(make_definition(
            actions=[{"type": "update_agent", "params": {}}]))[0])


class PlaybookAsFunctionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)

    def test_playbook_ref_expands_inline(self):
        inner = make_definition(actions=[{"type": "pkg_update", "params": {}}])
        make_playbook(self.admin, name="Common prep", definitions=[inner])
        actions, risk = expand_actions([
            {"type": "playbook", "params": {"name": "common PREP"}},  # case-insensitive
            {"type": "restart_service", "params": {"service_name": "app"}},
        ])
        self.assertEqual([a["type"] for a in actions],
                         ["pkg_update", "restart_service"])
        self.assertEqual(risk, "standard")

    def test_nested_playbooks_expand(self):
        leaf = make_definition(actions=[{"type": "pkg_update", "params": {}}])
        make_playbook(self.admin, name="Leaf", definitions=[leaf])
        mid = make_definition(actions=[{"type": "playbook", "params": {"name": "Leaf"}}])
        make_playbook(self.admin, name="Mid", definitions=[mid])
        actions, _ = expand_actions([{"type": "playbook", "params": {"name": "Mid"}}])
        self.assertEqual([a["type"] for a in actions], ["pkg_update"])

    def test_cycles_are_refused(self):
        d = make_definition(actions=[{"type": "playbook", "params": {"name": "Ouro"}}])
        make_playbook(self.admin, name="Ouro", definitions=[d])
        with self.assertRaises(PlaybookExpandError):
            expand_actions([{"type": "playbook", "params": {"name": "Ouro"}}])

    def test_unknown_playbook_is_an_error(self):
        with self.assertRaises(PlaybookExpandError):
            expand_actions([{"type": "playbook", "params": {"name": "ghost"}}])

    def test_disabled_playbook_is_still_callable(self):
        inner = make_definition()
        make_playbook(self.admin, name="Retired", definitions=[inner], auto_enroll=False)
        actions, _ = expand_actions([{"type": "playbook", "params": {"name": "Retired"}}])
        self.assertEqual(len(actions), 1)

    def test_risk_escalates_to_max_of_expansion(self):
        risky = make_definition(risk="standard")
        make_playbook(self.admin, name="Std", definitions=[risky])
        _, risk = expand_actions([{"type": "playbook", "params": {"name": "Std"}}])
        self.assertEqual(risk, "standard")


class PlaybookApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True)
        self.client.force_login(self.admin)

    def test_create_sequence_and_reorder(self):
        d1, d2 = make_definition(name="A"), make_definition(name="B")
        resp = self.client.post("/api/v1/playbooks/", {
            "name": "Bootstrap", "definition_ids": [str(d1.id), str(d2.id)],
            "target_tags": ["os:linux"]}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        bid = resp.json()["id"]
        self.assertEqual([s["definition_name"] for s in resp.json()["steps"]],
                         ["A", "B"])
        resp = self.client.patch(f"/api/v1/playbooks/{bid}/", {
            "definition_ids": [str(d2.id), str(d1.id)]},
            content_type="application/json")
        self.assertEqual([s["definition_name"] for s in resp.json()["steps"]],
                         ["B", "A"])

    def test_create_refuses_ineligible_definition(self):
        bad = make_definition(risk="high")
        resp = self.client.post("/api/v1/playbooks/", {
            "name": "Nope", "definition_ids": [str(bad.id)]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Playbook.objects.count(), 0)

    def test_duplicate_name_refused(self):
        make_playbook(self.admin, name="Taken")
        d = make_definition()
        resp = self.client.post("/api/v1/playbooks/", {
            "name": "taken", "definition_ids": [str(d.id)]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_step_params_override_round_trips(self):
        d = make_definition(actions=[{"type": "restart_service",
                                      "params": {"service_name": "nginx"}}])
        resp = self.client.post("/api/v1/playbooks/", {
            "name": "Overridden",
            "definition_ids": [{"definition_id": str(d.id),
                                "params_override": {"0": {"service_name": "postgres"}}}]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["steps"][0]["params_override"],
                         {"0": {"service_name": "postgres"}})
        got = self.client.get(f"/api/v1/playbooks/{resp.json()['id']}/").json()
        self.assertEqual(got["steps"][0]["params_override"],
                         {"0": {"service_name": "postgres"}})

    def test_step_params_override_applies_at_dispatch(self):
        from .models import build_agent_steps
        d = make_definition(actions=[
            {"type": "restart_service", "params": {"service_name": "nginx"}},
            {"type": "pkg_update", "params": {}},
        ])
        b = make_playbook(self.admin, definitions=[d])
        step = b.steps.get()
        step.params_override = {"0": {"service_name": "postgres"}}
        step.save()
        steps, _ = build_agent_steps(b)
        self.assertEqual(steps[0]["params"], {"service_name": "postgres"})
        self.assertEqual(steps[1]["params"], {})

    def test_unknown_override_param_is_refused(self):
        d = make_definition(actions=[{"type": "restart_service",
                                      "params": {"service_name": "nginx"}}])
        resp = self.client.post("/api/v1/playbooks/", {
            "name": "Bad override",
            "definition_ids": [{"definition_id": str(d.id),
                                "params_override": {"0": {"bogus": "x"}}}]},
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("bogus", resp.json()["detail"])
        self.assertEqual(Playbook.objects.count(), 0)

    def test_toggle_and_delete(self):
        b = make_playbook(self.admin)
        resp = self.client.patch(f"/api/v1/playbooks/{b.id}/", {"auto_enroll": False},
                                 content_type="application/json")
        self.assertFalse(resp.json()["auto_enroll"])
        self.assertEqual(self.client.delete(f"/api/v1/playbooks/{b.id}/").status_code, 204)


class PlaybookNameScopeTests(TestCase):
    def test_two_playbooks_may_share_a_name_at_the_database_level(self):
        """Uniqueness moves to the view layer so that, once scoped, two sites
        can each own a 'Nightly patch scan'."""
        Playbook.objects.create(name="Nightly patch scan")
        Playbook.objects.create(name="Nightly patch scan")   # must not raise
        self.assertEqual(Playbook.objects.filter(name="Nightly patch scan").count(), 2)

    def test_api_still_rejects_a_duplicate_name(self):
        user = get_user_model().objects.create_user("op", password="x", is_staff=True)
        self.client.force_login(user)
        Playbook.objects.create(name="Hardening")
        resp = self.client.post(
            "/api/v1/playbooks/",
            data=json.dumps({"name": "Hardening", "definition_ids": []}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_api_still_rejects_a_duplicate_name_on_rename(self):
        user = get_user_model().objects.create_user("op2", password="x", is_staff=True)
        self.client.force_login(user)
        Playbook.objects.create(name="Hardening")
        other = Playbook.objects.create(name="Patching")
        resp = self.client.patch(
            f"/api/v1/playbooks/{other.id}/",
            data=json.dumps({"name": "Hardening"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)


class HighRiskOptInTests(TestCase):
    """High-risk steps are allowed in a playbook only when it opted in, and
    opting in costs a TOTP code.

    A playbook dispatches unattended on every matching enrollment. Run by
    hand a high-risk task costs 2FA plus a 60-second delay with a human
    watching; here there is nobody. So the authorization happens once, in
    advance, at the moment the box is ticked.
    """

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.high = make_definition(risk="high")

    # ── the rule itself ────────────────────────────────────────────────
    def test_high_risk_is_refused_by_default(self):
        ok, why = eligible(self.high)
        self.assertFalse(ok)
        self.assertIn("high-risk", why)

    def test_high_risk_is_allowed_when_opted_in(self):
        ok, _ = eligible(self.high, allow_high_risk=True)
        self.assertTrue(ok)

    def test_update_agent_stays_refused_even_when_opted_in(self):
        """It replaces the executable that enforces the agent's allowlist —
        the opt-in is about risk tier, not about that."""
        definition = make_definition(
            risk="standard", actions=[{"type": "update_agent", "params": {}}])
        ok, why = eligible(definition, allow_high_risk=True)
        self.assertFalse(ok)
        self.assertIn("update_agent", why)

    def test_the_default_is_off(self):
        """The signature's default is what protects every caller that has
        not been taught about the flag."""
        self.assertFalse(Playbook().allow_high_risk)

    # ── dispatch honours the flag ──────────────────────────────────────
    def test_dispatch_skips_high_risk_steps_without_the_opt_in(self):
        playbook = make_playbook(self.admin, definitions=[self.high])
        playbook.allow_high_risk = False
        playbook.save()
        created = dispatch_to_host(make_host(), playbooks=[playbook])
        self.assertEqual(created, 0)

    def test_dispatch_runs_high_risk_steps_once_opted_in(self):
        """The half-working case this guards: saving succeeds, and then
        dispatch silently skips the playbook forever."""
        playbook = make_playbook(self.admin, definitions=[self.high])
        playbook.allow_high_risk = True
        playbook.save()
        created = dispatch_to_host(make_host(), playbooks=[playbook])
        self.assertEqual(created, 1)

    # ── the API gate ───────────────────────────────────────────────────
    def _post(self, **extra):
        body = {"name": f"bl-{uuid.uuid4().hex[:6]}",
                "definition_ids": [str(self.high.id)], **extra}
        return self.client.post("/api/v1/playbooks/", json.dumps(body),
                                content_type="application/json")

    def test_creating_with_high_risk_steps_is_refused_without_the_flag(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("high-risk", resp.json()["detail"])

    def test_setting_the_flag_without_a_totp_code_is_refused(self):
        resp = self._post(allow_high_risk=True)
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertTrue(resp.json().get("needs_totp"))
        self.assertFalse(Playbook.objects.exists(),
                         "a refused create must leave no playbook behind")

    def test_a_bad_totp_code_is_refused(self):
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value="Invalid TOTP code"):
            resp = self._post(allow_high_risk=True, totp="000000")
        self.assertEqual(resp.status_code, 403)

    def test_a_good_totp_code_creates_the_playbook_with_high_risk_steps(self):
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self._post(allow_high_risk=True, totp="123456")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.json()["allow_high_risk"])
        self.assertTrue(Playbook.objects.get().allow_high_risk)

    def test_the_flag_is_set_before_steps_are_validated(self):
        """Order matters: eligibility is judged against the flag, so setting
        it after validation would reject the very steps it permits."""
        with patch("apps.accounts.totp.require_totp_confirmation",
                   return_value=None):
            resp = self._post(allow_high_risk=True, totp="123456")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Playbook.objects.get().steps.count(), 1)

    def test_turning_the_flag_on_by_patch_needs_a_code(self):
        playbook = make_playbook(self.admin, definitions=[make_definition()])
        resp = self.client.patch(
            f"/api/v1/playbooks/{playbook.id}/",
            json.dumps({"allow_high_risk": True}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        playbook.refresh_from_db()
        self.assertFalse(playbook.allow_high_risk)

    def test_turning_the_flag_off_needs_no_code(self):
        """Withdrawing an authorization requires no authorization."""
        playbook = make_playbook(self.admin, definitions=[make_definition()])
        playbook.allow_high_risk = True
        playbook.save()
        resp = self.client.patch(
            f"/api/v1/playbooks/{playbook.id}/",
            json.dumps({"allow_high_risk": False}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        playbook.refresh_from_db()
        self.assertFalse(playbook.allow_high_risk)

    def test_an_unrelated_patch_does_not_re_prompt(self):
        """Editing the name of an already-authorized playbook must not spend
        a code — they are single-use inside their validity window."""
        playbook = make_playbook(self.admin, definitions=[self.high])
        playbook.allow_high_risk = True
        playbook.save()
        resp = self.client.patch(
            f"/api/v1/playbooks/{playbook.id}/",
            json.dumps({"description": "renamed"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        playbook.refresh_from_db()
        self.assertTrue(playbook.allow_high_risk)

    def test_the_flag_is_reported_in_the_row(self):
        playbook = make_playbook(self.admin, definitions=[make_definition()])
        resp = self.client.get("/api/v1/playbooks/")
        row = next(r for r in resp.json() if r["id"] == str(playbook.id))
        self.assertIn("allow_high_risk", row)
        self.assertFalse(row["allow_high_risk"])
