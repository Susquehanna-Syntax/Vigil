"""M10: a detection task's result becomes a finding with its evidence."""
import json

from django.test import TestCase

from apps.hosts.models import Host
from apps.tasks.dispatch import task_params
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import parse_and_validate
from apps.tasks.test_detection_spec import LOG4SHELL

from .models import VulnFinding, VulnSummary


class DetectionFindingTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="app-01", agent_token="d" * 32,
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
        spec = parse_and_validate(LOG4SHELL)
        self.definition = TaskDefinition.objects.create(name=spec["name"], yaml_source=LOG4SHELL,
                                                        parsed_spec=spec)
        self.params = task_params(spec)[0]
        self.n = 0

    def _report(self, state, steps):
        self.n += 1
        run = TaskRun.objects.create(definition=self.definition, host_count=1)
        task = Task.objects.create(host=self.host, run=run, action="_script", params=self.params,
                                   nonce=f"{self.n}" * 64, state=Task.State.DISPATCHED)
        resp = self.client.post("/api/v1/tasks/result/", data=json.dumps(
            {"task_id": str(task.id), "state": state, "output": "x", "steps": steps}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.host.agent_token}")
        self.assertEqual(resp.status_code, 200, resp.content)

    def _matched_steps(self, fix_status="ok", boost=True):
        jar = {"evidence_type": "file", "path": "/opt/app/log4j-core-2.14.1.jar", "size": 1}
        java = {"evidence_type": "process", "pid": 9, "name": "java", "cmdline": "java -jar app"}
        steps = [{"id": "relevant-1", "status": "ok", "result": {"count": 1},
                  "hunt": {"matches": [jar]}}]
        if boost:
            steps.append({"id": "boost-1", "status": "ok", "result": {"count": 1},
                          "hunt": {"matches": [java]}})
        steps.append({"id": "detection", "status": "ok",
                      "result": {"confidence": "high" if boost else "normal", "boost_matches": int(boost)}})
        steps.append({"id": "remove", "status": fix_status})
        return steps

    def test_failed_fix_leaves_an_open_finding_with_evidence(self):
        self._report("failed", self._matched_steps(fix_status="failed"))
        f = VulnFinding.objects.get(host=self.host)
        self.assertEqual((f.scanner, f.cve_id, f.severity, f.state),
                         ("detection", "CVE-2021-44228", "critical", "open"))
        self.assertEqual(f.fix_key, f"det:{self.definition.id}")
        self.assertEqual(f.advisory["confidence"], "high")
        kinds = {e.kind: e.summary for e in f.evidence.all()}
        self.assertEqual(kinds["file"], "file /opt/app/log4j-core-2.14.1.jar")
        self.assertEqual(kinds["process"], "boost: process java")
        self.assertEqual(VulnSummary.objects.get(host=self.host).critical, 1)

    def test_completed_records_it_found_and_fixed(self):
        self._report("completed", self._matched_steps())
        f = VulnFinding.objects.get(host=self.host)
        self.assertEqual(f.state, "fixed")
        self.assertIsNotNone(f.resolved_at)

    def test_not_applicable_closes_an_earlier_finding(self):
        self._report("failed", self._matched_steps(fix_status="failed"))
        self._report("not_applicable", [{"id": "relevant-1", "status": "ok", "result": {"count": 0},
                                         "hunt": {"matches": []}}])
        self.assertEqual(VulnFinding.objects.get(host=self.host).state, "fixed")

    def test_an_ordinary_task_raises_nothing(self):
        spec = parse_and_validate("name: plain\nactions:\n  - type: app_inventory\n")
        self.definition = TaskDefinition.objects.create(name="plain", yaml_source="", parsed_spec=spec)
        self.params = task_params(spec)[0]
        self._report("failed", [])
        self.assertFalse(VulnFinding.objects.exists())
