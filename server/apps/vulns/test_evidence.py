"""M9 evidence: what a finding rests on, and the confidence it adds up to."""
from datetime import timedelta

from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.tasks.models import HuntMatch, Task

from .evidence import add_evidence, attach_runtime_evidence, confidence, confidence_for
from .models import FindingEvidence, VulnFinding

K = FindingEvidence.Kind


def _finding(host, pkg="openssl", path=""):
    return VulnFinding.objects.create(
        host=host, scanner="vigil", plugin_id_or_oid=f"{pkg}:CVE-1", cve_id="CVE-2024-0001",
        severity="high", package_name=pkg, affected_path=path)


class ConfidenceTests(TestCase):
    def test_table(self):
        self.assertEqual(confidence_for([]), "reported")
        self.assertEqual(confidence_for([K.SCANNER]), "reported")
        self.assertEqual(confidence_for([K.FILE]), "file")
        self.assertEqual(confidence_for([K.PACKAGE]), "confirmed")
        self.assertEqual(confidence_for([K.MISSING_UPDATE]), "confirmed")
        self.assertEqual(confidence_for([K.PACKAGE, K.PROCESS]), "urgent")
        self.assertEqual(confidence_for([K.REGISTRY, K.PORT]), "urgent")
        self.assertEqual(confidence_for([K.FILE, K.PROCESS]), "file",
                         "a running process alone never makes a leftover file urgent")


class RuntimeEvidenceTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="x" * 32,
                                        status=Host.Status.ONLINE)
        self.task = Task.objects.create(host=self.host, action="_script", nonce="n" * 64)

    def _match(self, kind, days_ago=0, **data):
        m = HuntMatch.objects.create(task=self.task, host=self.host, step_id="s", action=f"hunt_{kind}",
                                     evidence_type=kind, data=data)
        HuntMatch.objects.filter(pk=m.pk).update(created_at=now() - timedelta(days=days_ago))

    def test_process_and_port_by_name(self):
        f = _finding(self.host, pkg="nginx")
        add_evidence(f, K.PACKAGE, "dpkg:nginx", "nginx 1.18 installed")
        self._match("process", pid=42, name="nginx", cmdline="nginx: master process")
        self._match("port", port=443, protocol="tcp", process="nginx", address="0.0.0.0")
        self._match("process", pid=7, name="sshd", cmdline="/usr/sbin/sshd")
        self.assertEqual(attach_runtime_evidence(self.host), 2)
        self.assertEqual(sorted(e.kind for e in f.evidence.all()), ["package", "port", "process"])
        self.assertEqual(confidence(f), "urgent")
        # Idempotent: a second pass refreshes, it does not duplicate.
        attach_runtime_evidence(self.host)
        self.assertEqual(f.evidence.count(), 3)

    def test_jar_by_affected_file_and_stale_matches_ignored(self):
        f = _finding(self.host, pkg="org.apache.logging.log4j:log4j-core",
                     path="opt/app/lib/log4j-core-2.14.1.jar")
        self._match("process", days_ago=30, pid=1, name="java",
                    cmdline="java -cp log4j-core-2.14.1.jar")
        self.assertEqual(attach_runtime_evidence(self.host), 0, "a month-old hunt is not 'running now'")
        self._match("process", pid=2, name="java", cmdline="java -cp /opt/app/lib/log4j-core-2.14.1.jar")
        self.assertEqual(attach_runtime_evidence(self.host), 1)

    def test_findings_api_carries_evidence_and_confidence(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        from apps.accounts.models import Role, UserProfile
        f = _finding(self.host)
        add_evidence(f, K.FILE, "/opt/x/libssl.so.1.1", "libssl.so.1.1 on disk")
        user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        client = APIClient()
        client.force_authenticate(user)
        rows = client.get("/api/v1/vulns/findings/").json()
        rows = rows if isinstance(rows, list) else rows["results"]
        self.assertEqual(rows[0]["confidence"], "file")
        self.assertEqual(rows[0]["evidence"][0]["key"], "/opt/x/libssl.so.1.1")
