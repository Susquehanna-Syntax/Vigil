"""Warn before deploying to hosts whose agent would refuse the task (M7 phase 08).

The agent reports its allowlist at check-in; the server works out which targets
would refuse, skips them by default (reporting them), and sends anyway when told.
"""
import re
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition
from apps.tasks.refusals import AGENT_ACTIONS, refused_actions, task_action_types
from apps.tasks.spec import parse_and_validate

CHECK = """name: Check cron
risk: low
actions:
  - id: c
    type: check_service
    params:
      service_name: cron
"""

RELEVANT = """name: Check nginx where it runs
risk: low
relevant:
  all:
    - hunt_service:
        name: nginx
actions:
  - id: c
    type: check_service
    params:
      service_name: nginx
"""


def _host(name, mode="managed", allowlist=("check_service",), reprovision=False):
    return Host.objects.create(hostname=name, agent_token=f"tok-{name}", status=Host.Status.ONLINE,
                               mode=mode, agent_allowlist=None if allowlist is None else list(allowlist),
                               agent_allow_reprovision=reprovision)


class RefusedActionsTests(TestCase):
    def test_refused_actions_per_mode(self):
        wanted = {"check_service", "hunt_service", "run_command", "reprovision_stage", "playbook"}
        self.assertEqual(refused_actions(_host("m1", mode="monitor"), wanted),
                         ["check_service", "hunt_service", "reprovision_stage", "run_command"])
        self.assertEqual(refused_actions(_host("m2"), wanted),
                         ["hunt_service", "reprovision_stage", "run_command"])
        self.assertEqual(refused_actions(_host("m3", allowlist=("check_service", "hunt_service"),
                                               reprovision=True), wanted), ["run_command"])
        self.assertEqual(refused_actions(_host("m4", mode="full_control"), wanted), ["reprovision_stage"])
        self.assertEqual(refused_actions(_host("m5", mode="full_control", reprovision=True), wanted), [])
        # An agent that never reported an allowlist is unknown, not refusing.
        self.assertEqual(refused_actions(_host("m6", allowlist=None), wanted), [])

    def test_unprivileged_managed_host_refuses_everything(self):
        """QA-08: an agent still running under the monitor-mode unit's sandbox
        fails every task it accepts, so it refuses all of them up front."""
        wanted = {"check_service", "hunt_service", "run_command", "reprovision_stage", "playbook"}
        every = sorted(wanted & AGENT_ACTIONS)

        host = _host("p1", allowlist=("check_service", "hunt_service"), reprovision=True)
        host.agent_runs_as_root = False
        host.save(update_fields=["agent_runs_as_root"])
        self.assertEqual(refused_actions(host, wanted), every)

        host.mode = Host.Mode.FULL_CONTROL
        host.save(update_fields=["mode"])
        self.assertEqual(refused_actions(host, wanted), every)

        # Unknown (None) is not "refuses" — an old agent must not look broken.
        host.agent_runs_as_root = None
        host.save(update_fields=["agent_runs_as_root"])
        self.assertEqual(refused_actions(host, wanted), [])

    def test_task_action_types_includes_relevant_probes(self):
        self.assertEqual(task_action_types(parse_and_validate(RELEVANT)),
                         {"check_service", "hunt_service"})

    def test_server_copy_matches_the_agent(self):
        src = (Path(__file__).resolve().parents[3] / "agent" / "vigil_agent" / "config.py").read_text()
        block = re.search(r"_ALL_ACTIONS = \{(.*?)\n\}", src, re.S).group(1)
        agent = set(re.findall(r'"([a-z_]+)"', block))
        self.assertEqual(AGENT_ACTIONS - {"run_command", "reprovision_stage", "reprovision_commit",
                                          "reprovision_cleanup"}, agent)


class DeploySkipsRefusingHostsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ref", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.definition = TaskDefinition.objects.create(
            owner=self.user, name="Check cron", yaml_source=CHECK, parsed_spec=parse_and_validate(CHECK))
        self.ok = _host("allows", allowlist=("check_service",))
        self.refuses = _host("refuses", allowlist=("hunt_file",))

    def _deploy(self, hosts, **extra):
        with patch("apps.accounts.totp.require_totp_confirmation", return_value=None):
            return self.client.post(f"/api/v1/tasks/definitions/{self.definition.id}/deploy/",
                                    {"host_ids": [str(h.id) for h in hosts], "totp": "123456", **extra},
                                    content_type="application/json")

    def test_deploy_skips_refusing_hosts(self):
        resp = self._deploy([self.ok, self.refuses])
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["host_count"], 1)
        self.assertEqual(body["skipped"], [{"host_id": str(self.refuses.id), "hostname": "refuses",
                                            "actions": ["check_service"]}])

        resp = self._deploy([self.ok, self.refuses], send_to_refusing=True)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual((resp.json()["host_count"], resp.json()["skipped"]), (2, []))

        resp = self._deploy([self.refuses])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["skipped"][0]["hostname"], "refuses")

    def test_refusals_endpoint(self):
        resp = self.client.get(f"/api/v1/tasks/definitions/{self.definition.id}/refusals/",
                               {"host_ids": f"{self.ok.id},{self.refuses.id},not-a-uuid"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([r["hostname"] for r in resp.json()["refusals"]], ["refuses"])


class CheckinStoresAllowlistTests(TestCase):
    def _checkin(self, host, **payload):
        return self.client.post("/api/v1/checkin", {"hostname": host.hostname, "metrics": {}, **payload},
                                content_type="application/json",
                                HTTP_AUTHORIZATION=f"Bearer {host.raw_agent_token}")

    def test_checkin_stores_allowlist(self):
        host = _host("ci", allowlist=None)
        self._checkin(host, allowlist=["hunt_file", "check_service", "hunt_file"], allow_reprovision=True)
        host.refresh_from_db()
        self.assertEqual(host.agent_allowlist, ["check_service", "hunt_file"])
        self.assertTrue(host.agent_allow_reprovision)
        self._checkin(host, allowlist="everything")          # malformed: kept
        self._checkin(host)                                   # absent: kept
        host.refresh_from_db()
        self.assertEqual(host.agent_allowlist, ["check_service", "hunt_file"])

    def test_checkin_stores_runs_as_root(self):
        """QA-08: the server needs the agent's privilege to warn about a host
        whose service is still the monitor-mode unit."""
        host = _host("rr")
        self._checkin(host, runs_as_root=False)
        host.refresh_from_db()
        self.assertIs(host.agent_runs_as_root, False)
        self._checkin(host, runs_as_root="no")               # malformed: kept
        self._checkin(host)                                   # absent: kept
        host.refresh_from_db()
        self.assertIs(host.agent_runs_as_root, False)
