import hashlib
from uuid import uuid4

from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.relays.models import Relay

from . import render
from .models import AgentProfile, EnrollConfig


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_relay(name="r1", url="https://relay.example.com/", enabled=True):
    return Relay.objects.create(name=name, url=url.rstrip("/"),
                                kind=Relay.Kind.REMOTE, enabled=enabled)


def as_user(client, role=Role.ADMIN, username="admin"):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username)
    UserProfile.objects.create(user=user, role=role)
    client.force_authenticate(user)
    return user


def base_profile(**over):
    data = {"name": "web", "mode": "monitor"}
    data.update(over)
    return data


class BuiltinProfileTests(TestCase):
    def test_the_three_builtins_exist_after_migrations(self):
        names = list(AgentProfile.objects.filter(builtin=True)
                     .values_list("name", flat=True))
        self.assertEqual(names, ["Full control workstation", "Managed server",
                                 "Monitor only"])

    def test_managed_builtin_allowlist_is_all_known_actions(self):
        from apps.tasks.refusals import AGENT_ACTIONS

        profile = AgentProfile.objects.get(name="Managed server")
        self.assertEqual(profile.mode, "managed")
        self.assertTrue(profile.allowlist)
        self.assertTrue(set(profile.allowlist) <= set(AGENT_ACTIONS))
        self.assertIn("restart_service", profile.allowlist)

    def test_full_control_builtin_turns_both_session_switches_on(self):
        profile = AgentProfile.objects.get(name="Full control workstation")
        self.assertEqual(profile.mode, "full_control")
        self.assertTrue(profile.allow_remote_shell)
        self.assertTrue(profile.allow_remote_desktop)


class ProfileApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        as_user(self.client)

    def test_crud_round_trip(self):
        created = self.client.post("/api/v1/agent-profiles/", base_profile(
            name="ops", mode="managed", allowlist=["restart_service"]),
            content_type="application/json")
        self.assertEqual(created.status_code, 201)
        profile_id = created.json()["id"]
        self.assertEqual(created.json()["allowlist"], ["restart_service"])
        self.assertEqual(created.json()["install_profiles"], [])

        listed = self.client.get("/api/v1/agent-profiles/").json()
        self.assertEqual([p["name"] for p in listed][:1],
                         ["Full control workstation"])
        patched = self.client.patch(f"/api/v1/agent-profiles/{profile_id}/",
                                    {"description": "ops boxes"},
                                    content_type="application/json")
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["description"], "ops boxes")
        # PATCH validates the merged result, not just the diff.
        bad = self.client.patch(f"/api/v1/agent-profiles/{profile_id}/",
                                {"mode": "sideways"},
                                content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("mode", bad.json()["errors"])

        self.assertEqual(
            self.client.delete(f"/api/v1/agent-profiles/{profile_id}/").status_code,
            204)
        self.assertFalse(AgentProfile.objects.filter(pk=profile_id).exists())

    def test_a_builtin_can_be_edited_but_not_deleted(self):
        builtin = AgentProfile.objects.get(name="Monitor only")
        refused = self.client.delete(f"/api/v1/agent-profiles/{builtin.id}/")
        self.assertEqual(refused.status_code, 400)
        self.assertTrue(AgentProfile.objects.filter(pk=builtin.id).exists())

        ok = self.client.patch(f"/api/v1/agent-profiles/{builtin.id}/",
                               {"description": "read-only boxes"},
                               content_type="application/json")
        self.assertEqual(ok.status_code, 200)

    def test_create_rejects_an_invalid_profile(self):
        resp = self.client.post("/api/v1/agent-profiles/",
                                base_profile(checkin_interval=5),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("checkin_interval", resp.json()["errors"])

    def test_actions_lists_allowlistable_and_never_allowlistable(self):
        actions = {a["name"]: a for a in
                   self.client.get("/api/v1/agent-profiles/actions/").json()}
        self.assertTrue(actions["restart_service"]["allowlistable"])
        self.assertFalse(actions["run_command"]["allowlistable"])
        self.assertEqual(actions["restart_service"]["group"], "service")
        self.assertEqual(actions["restart_service"]["label"], "Restart service")


class EnrollConfigApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        as_user(self.client)

    def test_post_returns_a_token_that_honours_the_overrides(self):
        profile = AgentProfile.objects.create(name="web", mode="managed",
                                             allowlist=["restart_service"])
        resp = self.client.post("/api/v1/agent-profiles/enroll-config/",
                                {"profile": str(profile.id),
                                 "overrides": {"mode": "full_control"}},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(len(body["token"]), 43)
        self.assertEqual(body["settings"]["mode"], "full_control")
        self.assertEqual(body["server_url"], "http://testserver")
        self.assertNotIn("allowlist", body["settings"])
        config = EnrollConfig.objects.get()
        self.assertNotEqual(config.token_hash, body["token"])

    def test_errors_come_back_and_nothing_is_created(self):
        resp = self.client.post("/api/v1/agent-profiles/enroll-config/",
                                {"overrides": {"tags": ["not ok!"]}},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("tags", resp.json()["errors"])
        self.assertEqual(EnrollConfig.objects.count(), 0)

    def test_a_one_off_config_needs_no_profile(self):
        resp = self.client.post("/api/v1/agent-profiles/enroll-config/",
                                {"overrides": {"mode": "managed",
                                               "allowlist": ["restart_service"]}},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["settings"]["allowlist"], ["restart_service"])

    def test_a_profile_id_that_is_not_one_is_a_400(self):
        resp = self.client.post("/api/v1/agent-profiles/enroll-config/",
                                {"profile": "not-a-uuid"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("profile", resp.json()["errors"])

    def test_a_viewer_cannot_read_profiles(self):
        as_user(self.client, Role.VIEWER, "watcher")
        self.assertEqual(
            self.client.get("/api/v1/agent-profiles/").status_code, 403)
        self.assertEqual(self.client.post(
            "/api/v1/agent-profiles/enroll-config/", {},
            content_type="application/json").status_code, 403)

class RedemptionTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _mint(self, **over):
        from apps.agentprofiles.views import CONFIG_TOKEN_TTL

        clean, errors = render.validate(base_profile(
            name="web", mode="managed", allowlist=["restart_service"], **over))
        self.assertEqual(errors, {})
        token = "tok-" + uuid4().hex
        config = EnrollConfig.objects.create(
            token_hash=_hash(token), profile=None,
            settings=render.agent_settings(clean, "https://vigil.example.com"),
            expires_at=now() + CONFIG_TOKEN_TTL)
        return token, config

    def test_first_get_is_yaml_the_second_is_404(self):
        token, config = self._mint()
        resp = self.client.get(f"/agent/enroll-config/{token}/",
                               REMOTE_ADDR="203.0.113.7")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/yaml; charset=utf-8")
        text = resp.content.decode()
        self.assertIn('agent_token: "REPLACE_WITH_TOKEN"', text)
        self.assertIn("mode: managed", text)
        self.assertIn("- restart_service", text)
        self.assertIn("# Vigil agent config", text)

        config.refresh_from_db()
        self.assertIsNotNone(config.used_at)
        self.assertEqual(config.used_ip, "203.0.113.7")

        again = self.client.get(f"/agent/enroll-config/{token}/")
        self.assertEqual(again.status_code, 404)
        self.assertIn(b"Unknown or used config token.", again.content)

    def test_expired_token_404(self):
        token, _ = self._mint()
        EnrollConfig.objects.update(expires_at=now())
        self.assertEqual(
            self.client.get(f"/agent/enroll-config/{token}/").status_code, 404)

    def test_malformed_tokens_never_reach_the_database(self):
        self._mint()
        for bad in ["has/slash", "x" * 200, "has space"]:
            resp = self.client.get(f"/agent/enroll-config/{bad}/")
            self.assertEqual(resp.status_code, 404, bad)

    def test_path_is_an_agent_path_for_the_relay_gate(self):
        builtin = Relay.objects.get(kind=Relay.Kind.BUILTIN)
        builtin.enabled = False
        builtin.save(update_fields=["enabled"])
        token, _ = self._mint()
        resp = self.client.get(f"/agent/enroll-config/{token}/")
        resp = self.client.get(f"/agent/enroll-config/{token}/")
        self.assertEqual(resp.status_code, 403)


class ValidateTests(TestCase):
    def test_rejects_never_allowlistable_and_unknown_actions(self):
        _, errors = render.validate(base_profile(
            mode="managed", allowlist=["run_command"]))
        self.assertIn("allowlist", errors)
        _, errors = render.validate(base_profile(
            mode="managed", allowlist=["reprovision_stage"]))
        self.assertIn("allowlist", errors)
        _, errors = render.validate(base_profile(
            mode="managed", allowlist=["restart_servce"]))
        self.assertIn("allowlist", errors)
        clean, _ = render.validate(base_profile(
            mode="managed", allowlist=["restart_servce"]))
        # The error is what it is for: nothing half-valid lands in the profile.
        self.assertEqual(clean["allowlist"], [])

    def test_allowlist_is_deduplicated_and_sorted(self):
        clean, errors = render.validate(base_profile(
            mode="managed",
            allowlist=["stop_service", "restart_service", "stop_service"]))
        self.assertEqual(errors, {})
        self.assertEqual(clean["allowlist"], ["restart_service", "stop_service"])

    def test_rejects_a_disabled_relay(self):
        relay = make_relay("edge", "https://e.example.com", enabled=False)
        _, errors = render.validate(base_profile(primary_relay=str(relay.id)))
        self.assertIn("primary_relay", errors)
        _, errors = render.validate(
            base_profile(fallback_relays=[str(relay.id)]))
        self.assertIn("fallback_relays", errors)

    def test_rejects_a_relay_id_that_is_not_one(self):
        _, errors = render.validate(base_profile(primary_relay="42"))
        self.assertIn("primary_relay", errors)
        _, errors = render.validate(base_profile(fallback_relays=["edge"]))
        self.assertIn("fallback_relays", errors)

    def test_rejects_a_primary_that_is_also_a_fallback(self):
        relay = make_relay()
        _, errors = render.validate(base_profile(
            primary_relay=str(relay.id), fallback_relays=[str(relay.id)]))
        self.assertIn("fallback_relays", errors)

    def test_rejects_a_bad_tag(self):
        for bad in (["has space"], ["naïve"], ["x" * 65], "notalist",
                    [f"t{i}" for i in range(21)]):
            _, errors = render.validate(base_profile(tags=bad))
            self.assertIn("tags", errors, bad)

    def test_accepts_the_tag_shape_the_wiki_uses(self):
        clean, errors = render.validate(
            base_profile(tags=["role:web-server", "env.prod", "team_ops"]))
        self.assertEqual(errors, {})
        self.assertEqual(clean["tags"], ["role:web-server", "env.prod",
                                         "team_ops"])

    def test_rejects_out_of_range_integers_and_a_bad_username(self):
        _, errors = render.validate(base_profile(checkin_interval=5))
        self.assertIn("checkin_interval", errors)
        _, errors = render.validate(base_profile(docker_check_interval=299))
        self.assertIn("docker_check_interval", errors)
        _, errors = render.validate(base_profile(software_interval=899))
        self.assertIn("software_interval", errors)
        _, errors = render.validate(base_profile(remote_desktop_port=0))
        self.assertIn("remote_desktop_port", errors)
        _, errors = render.validate(base_profile(remote_shell_user="Root!"))
        self.assertIn("remote_shell_user", errors)

    def test_monitor_mode_forces_the_session_switches_off_with_a_note(self):
        clean, errors = render.validate(base_profile(
            mode="monitor", allow_remote_shell=True, allow_remote_desktop=True,
            allow_reprovision=True))
        self.assertEqual(errors, {})
        self.assertFalse(clean["allow_remote_shell"])
        self.assertFalse(clean["allow_remote_desktop"])
        self.assertFalse(clean["allow_reprovision"])
        self.assertEqual(len(clean["_notes"]), 3)

    def test_full_control_keeps_the_switches_it_was_given(self):
        clean, errors = render.validate(base_profile(
            mode="full_control", allow_remote_shell=True))
        self.assertEqual(errors, {})
        self.assertTrue(clean["allow_remote_shell"])
        self.assertNotIn("_notes", clean)


class AgentSettingsTests(TestCase):
    def test_primary_relay_plus_two_fallbacks_then_the_dashboard(self):
        primary = make_relay("primary", "https://p.example.com")
        first = make_relay("first", "https://f1.example.com")
        second = make_relay("second", "https://f2.example.com")
        clean, errors = render.validate(base_profile(
            primary_relay=str(primary.id),
            fallback_relays=[str(first.id), str(second.id)]))
        self.assertEqual(errors, {})
        settings = render.agent_settings(clean, "https://vigil.example.com")
        self.assertEqual(settings["server_url"], "https://p.example.com")
        self.assertEqual(settings["fallback_urls"], [
            "https://f1.example.com", "https://f2.example.com",
            "https://vigil.example.com"])

    def test_dashboard_fallback_can_be_declined(self):
        primary = make_relay("primary", "https://p.example.com")
        clean, _ = render.validate(base_profile(
            primary_relay=str(primary.id), include_dashboard_fallback=False))
        settings = render.agent_settings(clean, "https://vigil.example.com")
        self.assertEqual(settings["fallback_urls"], [])

    def test_an_http_dashboard_is_not_a_fallback_for_an_https_relay(self):
        # The agent refuses an http fallback under an https server_url, so
        # writing one would leave an agent that will not start.
        primary = make_relay("primary", "https://p.example.com")
        clean, _ = render.validate(base_profile(primary_relay=str(primary.id)))
        settings = render.agent_settings(clean, "http://vigil.lan:8000")
        self.assertEqual(settings["fallback_urls"], [])

    def test_no_primary_means_the_dashboard_and_no_fallbacks(self):
        clean, errors = render.validate(base_profile())
        self.assertEqual(errors, {})
        settings = render.agent_settings(clean, "https://vigil.example.com")
        self.assertEqual(settings["server_url"], "https://vigil.example.com")
        self.assertEqual(settings["fallback_urls"], [])

    def test_allowlist_appears_only_in_managed_mode(self):
        for mode in AgentProfile.MODES:
            clean, _ = render.validate(base_profile(
                mode=mode, allowlist=["restart_service"]))
            settings = render.agent_settings(clean, "https://vigil.example.com")
            if mode == "managed":
                self.assertEqual(settings["allowlist"], ["restart_service"])
            else:
                self.assertNotIn("allowlist", settings)

    def test_yaml_holds_the_header_token_and_key_order(self):
        clean, _ = render.validate(base_profile(
            name="web", mode="managed", allowlist=["restart_service"]))
        text = render.to_yaml(
            render.agent_settings(clean, "https://vigil.example.com"))
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("# Vigil agent config"))
        self.assertEqual(lines[1], "server_url: https://vigil.example.com")
        self.assertEqual(lines[2], 'agent_token: "REPLACE_WITH_TOKEN"')
