"""Playbook inputs, and the callers that pass them.

A playbook declares its own inputs; each step maps them onto its task's
declared inputs (or a task input with the same id takes the playbook value);
and a caller — an automation, a rollout, a task's inline ``type: playbook``
action — supplies the values. Auto-enrolment supplies none, so a playbook with
inputs cannot auto-enrol.
"""
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.automations.engine import _build_for
from apps.automations.models import Automation
from apps.playbooks.community_yaml import parse, to_yaml
from apps.playbooks.inputs import PlaybookInputError, playbook_values
from apps.playbooks.models import Playbook, PlaybookStep
from apps.tasks.dispatch import build_playbook_steps, resolve_task_spec
from apps.tasks.models import TaskDefinition
from apps.tasks.views import _save_definition_from_yaml

JOIN = """
name: Join domain
risk: low
inputs:
  - {id: domain_name, label: Domain, type: text, required: true}
  - {id: retries, label: Retries, type: number, default: 2}
actions:
  - id: join
    type: restart_service
    params:
      service_name: ${{ inputs.domain_name }}
"""
PLAIN = """
name: Restart cron
risk: low
actions:
  - id: r
    type: restart_service
    params:
      service_name: cron
"""
DECLARES_DOMAIN = """
name: Says the domain
risk: low
inputs:
  - {id: domain, label: Domain, type: text, required: true}
actions:
  - id: say
    type: restart_service
    params:
      service_name: ${{ inputs.domain }}
"""
INLINE = """
name: Runs a playbook
risk: low
inputs:
  - {id: t, label: Target, type: text, required: true}
actions:
  - id: pb
    type: playbook
    params:
      name: PB
      inputs:
        domain: ${{ inputs.t }}
"""


class PlaybookInputsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("pi", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)

    def definition(self, yaml_text):
        d = TaskDefinition(owner=self.user)
        _save_definition_from_yaml(d, yaml_text)
        d.save()
        return d

    def playbook(self, *, name="PB", inputs=None, steps=(), auto_enroll=False):
        pb = Playbook.objects.create(name=name, created_by=self.user,
                                     inputs=inputs or [], auto_enroll=auto_enroll)
        for i, (d, step_inputs) in enumerate(steps):
            PlaybookStep.objects.create(playbook=pb, definition=d, order=i,
                                        inputs=step_inputs or {})
        return pb

    # -- dispatch ----------------------------------------------------------

    def test_step_mapping_reaches_the_task(self):
        join = self.definition(JOIN)
        pb = self.playbook(
            inputs=[{"id": "domain", "type": "text", "required": True}],
            steps=[(join, {"domain_name": "${{ inputs.domain }}"})])
        built = build_playbook_steps(pb, user=self.user, inputs={"domain": "corp"})
        self.assertEqual(len(built), 1)
        params = built[0][1]
        self.assertEqual(params["steps"][0]["params"]["service_name"], "corp")
        # The task's own default still applies to the input nobody mapped.
        self.assertEqual(params["steps"][0]["action"], "restart_service")

    def test_same_id_passes_through_without_a_mapping(self):
        says = self.definition(DECLARES_DOMAIN)
        pb = self.playbook(
            inputs=[{"id": "domain", "type": "text", "required": True}],
            steps=[(says, {})])
        built = build_playbook_steps(pb, user=self.user, inputs={"domain": "corp"})
        self.assertEqual(built[0][1]["steps"][0]["params"]["service_name"], "corp")

    def test_missing_required_playbook_input_is_refused(self):
        join = self.definition(JOIN)
        pb = self.playbook(
            inputs=[{"id": "domain", "type": "text", "required": True}],
            steps=[(join, {"domain_name": "${{ inputs.domain }}"})])
        with self.assertRaises(PlaybookInputError):
            build_playbook_steps(pb, user=self.user, inputs=None)
        with self.assertRaises(PlaybookInputError):
            playbook_values(pb, {})

    def test_inline_playbook_in_a_task_gets_inputs(self):
        self.playbook(
            inputs=[{"id": "domain", "type": "text", "required": True}],
            steps=[(self.definition(DECLARES_DOMAIN), {})])
        task = self.definition(INLINE)
        spec = resolve_task_spec(task, user=self.user, inputs={"t": "x"})
        actions = spec["actions"]
        self.assertEqual([a["type"] for a in actions], ["restart_service"])
        self.assertEqual(actions[0]["params"]["service_name"], "x")

    def test_inline_playbook_input_reaches_the_step(self):
        self.playbook(
            inputs=[{"id": "domain", "type": "text", "required": True}],
            steps=[(self.definition(DECLARES_DOMAIN), {})])
        task = self.definition(INLINE)
        spec = resolve_task_spec(task, user=self.user, inputs={"t": "example"})
        self.assertEqual(len(spec["actions"]), 1)
        self.assertEqual(
            spec["actions"][0]["params"]["service_name"], "example")

    # -- API ----------------------------------------------------------------

    def test_api_saves_inputs_and_rejects_unknown_refs(self):
        join = self.definition(JOIN)
        pb = self.playbook(steps=[])
        PlaybookStep.objects.create(playbook=pb, definition=join, order=0)
        bad = self.client.patch(
            f"/api/v1/playbooks/{pb.id}/",
            {"inputs": [{"id": "domain", "type": "text"}],
             "definition_ids": [{"definition_id": str(join.id),
                                 "inputs": {"domain_name": "${{ inputs.nope }}"}}]},
            content_type="application/json")
        self.assertEqual(bad.status_code, 400, bad.content[:300])
        pb.refresh_from_db()
        self.assertEqual(pb.inputs, [])

        good = self.client.patch(
            f"/api/v1/playbooks/{pb.id}/",
            {"inputs": [{"id": "domain", "label": "Domain", "type": "text"}],
             "definition_ids": [{"definition_id": str(join.id),
                                 "inputs": {"domain_name": "${{ inputs.domain }}"}}]},
            content_type="application/json")
        self.assertEqual(good.status_code, 200, good.content[:300])
        body = json.loads(good.content)
        self.assertEqual([i["id"] for i in body["inputs"]], ["domain"])
        self.assertEqual(body["steps"][0]["inputs"],
                         {"domain_name": "${{ inputs.domain }}"})

    def test_auto_enroll_needs_defaults(self):
        pb = self.playbook(auto_enroll=True)
        response = self.client.patch(
            f"/api/v1/playbooks/{pb.id}/",
            {"inputs": [{"id": "domain", "type": "text", "required": True}]},
            content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assertIn("auto-enrolment", json.loads(response.content)["detail"])
        pb.refresh_from_db()
        self.assertEqual(pb.inputs, [])

    def test_auto_enroll_accepts_inputs_that_all_have_defaults(self):
        pb = self.playbook(auto_enroll=True)
        response = self.client.patch(
            f"/api/v1/playbooks/{pb.id}/",
            {"inputs": [{"id": "domain", "type": "text", "required": True,
                         "default": "lab"}]},
            content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content[:300])

    # -- automations ---------------------------------------------------------

    def test_automation_passes_inputs(self):
        join = self.definition(JOIN)
        pb = self.playbook(
            inputs=[{"id": "domain", "type": "text", "required": True}],
            steps=[(join, {"domain_name": "${{ inputs.domain }}"})])
        automation = Automation.objects.create(
            name="A", created_by=self.user, trigger=Automation.Trigger.EVENT,
            event="alert_sent", action_kind=Automation.ActionKind.PLAYBOOK,
            playbook=pb, target="all", inputs={"domain": "corp"})
        built = _build_for(automation)
        self.assertIsNotNone(built)
        self.assertEqual(
            built[1][0][1]["steps"][0]["params"]["service_name"], "corp")

    def test_automation_with_inputs_the_playbook_cannot_use_is_refused(self):
        pb = self.playbook(
            inputs=[{"id": "domain", "type": "text", "required": True}], steps=[])
        response = self.client.post(
            "/api/v1/automations/",
            {"name": "A", "trigger": "event", "event": "alert_sent",
             "action_kind": "playbook", "playbook_name": pb.name,
             "target": "all", "inputs": {"nothing": "else"}},
            content_type="application/json")
        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assertFalse(Automation.objects.filter(name="A").exists())

    # -- YAML ----------------------------------------------------------------

    def test_yaml_round_trip_keeps_inputs(self):
        join = self.definition(JOIN)
        pb = self.playbook(name="Join everything",
                           inputs=[{"id": "domain", "type": "text",
                                    "label": "Domain", "required": True}],
                           steps=[(join, {"domain_name": "${{ inputs.domain }}"})])
        text = to_yaml(pb)
        self.assertIn("inputs:", text)
        parsed = parse(text)
        self.assertEqual([i["id"] for i in parsed["inputs"]], ["domain"])
        self.assertEqual(parsed["steps"][0]["inputs"],
                         {"domain_name": "${{ inputs.domain }}"})
