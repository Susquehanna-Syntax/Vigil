"""The YAML export/import endpoints for playbooks and automations.

The security question these exist to answer: importing a file must not be a
way around a gate the UI enforces. A playbook's ``allow_high_risk`` flag costs
a TOTP confirmation to turn on, because that one act authorizes every future
unattended dispatch — so a YAML file asking for it has to pay the same price.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.automations.models import Automation
from apps.tasks.models import TaskDefinition
from apps.tasks.spec import parse_and_validate

from .models import Playbook, PlaybookStep


def _definition(user, name, risk="low", action="clear_temp_files"):
    src = f"name: {name}\nrisk: {risk}\nactions:\n  - type: {action}\n"
    return TaskDefinition.objects.create(
        name=name, owner=user, yaml_source=src,
        parsed_spec=parse_and_validate(src), risk_level=risk)


class PlaybookYamlApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "ya", "ya@example.com", "x")
        self.client.force_login(self.user)
        self.definition = _definition(self.user, "Install Nginx")

    def _playbook(self, **kw):
        playbook = Playbook.objects.create(
            name=kw.pop("name", "Build"), created_by=self.user, **kw)
        PlaybookStep.objects.create(playbook=playbook,
                                    definition=self.definition, order=1)
        return playbook

    def test_export_returns_yaml_and_a_filename(self):
        playbook = self._playbook(name="Standard Linux Server Build")
        r = self.client.get(f"/api/v1/playbooks/{playbook.id}/yaml/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["filename"],
                         "standard-linux-server-build.yaml")
        self.assertIn("task: install-nginx", r.json()["yaml"])

    def test_import_creates_a_playbook(self):
        r = self.client.post("/api/v1/playbooks/yaml/", {
            "yaml": "name: Imported\ndescription: From a file.\n"
                    "target_tags:\n  - linux\nsteps:\n  - task: install-nginx\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:400])
        playbook = Playbook.objects.get(name="Imported")
        self.assertEqual(playbook.target_tags, ["linux"])
        self.assertEqual(
            [s.definition_id for s in playbook.steps.all()], [self.definition.id])

    def test_import_round_trips_an_export(self):
        playbook = self._playbook(name="Round Trip")
        exported = self.client.get(
            f"/api/v1/playbooks/{playbook.id}/yaml/").json()["yaml"]
        playbook.delete()
        r = self.client.post("/api/v1/playbooks/yaml/", {"yaml": exported},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:400])
        self.assertEqual(Playbook.objects.get(name="Round Trip").steps.count(), 1)

    def test_import_over_an_existing_playbook_replaces_it(self):
        playbook = self._playbook(name="Replace Me")
        r = self.client.post("/api/v1/playbooks/yaml/", {
            "playbook_id": str(playbook.id),
            "yaml": "name: Replace Me\ndescription: Now with a description.\n"
                    "steps:\n  - task: install-nginx\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:400])
        playbook.refresh_from_db()
        self.assertEqual(playbook.description, "Now with a description.")
        self.assertEqual(Playbook.objects.filter(name="Replace Me").count(), 1)

    def test_import_refuses_an_unknown_task_slug(self):
        r = self.client.post("/api/v1/playbooks/yaml/", {
            "yaml": "name: Broken\nsteps:\n  - task: nothing-like-this\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("nothing-like-this", r.json()["detail"])
        self.assertFalse(Playbook.objects.filter(name="Broken").exists())

    def test_import_refuses_a_duplicate_name(self):
        self._playbook(name="Taken")
        r = self.client.post("/api/v1/playbooks/yaml/", {
            "yaml": "name: Taken\nsteps:\n  - task: install-nginx\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Playbook.objects.filter(name="Taken").count(), 1)

    # ── The gate ────────────────────────────────────────────────────────────

    def test_yaml_import_cannot_turn_on_high_risk_without_totp(self):
        """The one that matters: a file must not be a door around the 2FA."""
        _definition(self.user, "Wipe It", risk="high")
        r = self.client.post("/api/v1/playbooks/yaml/", {
            "yaml": "name: Sneaky\nallow_high_risk: true\n"
                    "steps:\n  - task: install-nginx\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 403, r.content[:400])
        self.assertTrue(r.json().get("needs_totp"))
        self.assertFalse(
            Playbook.objects.filter(name="Sneaky").exists(),
            "the refused import must not leave a playbook behind")

    def test_a_refused_import_rolls_back_completely(self):
        """The gate fires mid-transaction, after the row is first written."""
        before = Playbook.objects.count()
        self.client.post("/api/v1/playbooks/yaml/", {
            "yaml": "name: Rollback\nallow_high_risk: true\n"
                    "steps:\n  - task: install-nginx\n",
        }, content_type="application/json")
        self.assertEqual(Playbook.objects.count(), before)

    def test_importing_a_high_risk_step_without_the_flag_is_refused(self):
        """Eligibility still applies to steps that arrive by file."""
        _definition(self.user, "Danger", risk="high")
        r = self.client.post("/api/v1/playbooks/yaml/", {
            "yaml": "name: Risky\nsteps:\n  - task: danger\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Playbook.objects.filter(name="Risky").exists())

    def test_export_requires_authentication(self):
        playbook = self._playbook()
        self.client.logout()
        r = self.client.get(f"/api/v1/playbooks/{playbook.id}/yaml/")
        self.assertIn(r.status_code, (401, 403))


class AutomationYamlApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            "aa", "aa@example.com", "x")
        self.client.force_login(self.user)
        self.definition = _definition(self.user, "Clear Temp Files")
        self.playbook = Playbook.objects.create(
            name="Container Host Maintenance", created_by=self.user)
        PlaybookStep.objects.create(playbook=self.playbook,
                                    definition=self.definition, order=1)

    def test_export_then_import_recreates_the_automation(self):
        automation = Automation.objects.create(
            name="Prune On Low Disk", trigger="event", event="alert_fired",
            min_severity="warning", action_kind="playbook",
            playbook=self.playbook, target="event_host", created_by=self.user)
        exported = self.client.get(
            f"/api/v1/automations/{automation.id}/yaml/").json()["yaml"]
        automation.delete()

        r = self.client.post("/api/v1/automations/yaml/", {"yaml": exported},
                             content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content[:400])
        rebuilt = Automation.objects.get(name="Prune On Low Disk")
        self.assertEqual(rebuilt.event, "alert_fired")
        self.assertEqual(rebuilt.playbook_id, self.playbook.id)

    def test_export_of_a_host_pinned_automation_is_a_400_with_a_reason(self):
        from apps.hosts.models import Host

        host = Host.objects.create(hostname="h", ip_address="10.0.0.7",
                                   agent_token="v" * 32)
        automation = Automation.objects.create(
            name="Pinned", trigger="event", event="alert_fired",
            action_kind="task", task_definition=self.definition,
            target="host", target_host=host, created_by=self.user)
        r = self.client.get(f"/api/v1/automations/{automation.id}/yaml/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("specific host", r.json()["detail"])

    def test_import_refuses_an_unknown_playbook_slug(self):
        r = self.client.post("/api/v1/automations/yaml/", {
            "yaml": "name: X\ntrigger: event\nevent: alert_fired\n"
                    "action_kind: playbook\nplaybook: not-here\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not-here", r.json()["detail"])

    def test_import_refuses_an_unknown_event(self):
        """_apply owns that rule; the YAML path must not skip it."""
        r = self.client.post("/api/v1/automations/yaml/", {
            "yaml": "name: X\ntrigger: event\nevent: not_a_real_event\n"
                    "action_kind: task\ntask: clear-temp-files\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_import_refuses_a_scheduled_automation_targeting_the_event_host(self):
        """There is no event, so there is no host — also _apply's rule."""
        r = self.client.post("/api/v1/automations/yaml/", {
            "yaml": "name: X\ntrigger: schedule\naction_kind: task\n"
                    "task: clear-temp-files\ntarget: event_host\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_import_clears_a_host_pin_it_replaces(self):
        """A shared file never names a machine, so a stale pin must not
        survive the import and keep silently targeting it."""
        from apps.hosts.models import Host

        host = Host.objects.create(hostname="h2", ip_address="10.0.0.8",
                                   agent_token="w" * 32)
        automation = Automation.objects.create(
            name="Repinned", trigger="event", event="alert_fired",
            action_kind="task", task_definition=self.definition,
            target="host", target_host=host, created_by=self.user)
        r = self.client.post("/api/v1/automations/yaml/", {
            "automation_id": str(automation.id),
            "yaml": "name: Repinned\ntrigger: event\nevent: alert_fired\n"
                    "action_kind: task\ntask: clear-temp-files\ntarget: all\n",
        }, content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content[:400])
        automation.refresh_from_db()
        self.assertEqual(automation.target, "all")
        self.assertIsNone(automation.target_host_id)
