"""M10: Anvil records — validated against Anvil's schema, imported as findings."""
import copy
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from .models import VulnFinding, VulnSummary
from .scanners.anvil import ingest_record, validate_record
from .scanners.trivy import ScanIngestError

TS = "2026-09-30T12:00:00Z"


def _result(n, *, host="build-runner-04", level="error", verdict="true_positive", kev=False,
            purl="pkg:deb/debian/openssl@1.1.1n-0+deb11u3", cves=("CVE-2022-0778",)):
    return {
        "ruleId": f"anvil.host.{n}", "level": level,
        "message": {"text": f"openssl vulnerable ({n})"},
        "locations": [{"logicalLocations": [{"name": host, "kind": "module"},
                                            {"fullyQualifiedName": purl, "kind": "package"}]}],
        "partialFingerprints": {"anvilFindingId/v1": f"{n:064x}"},
        "properties": {
            "anvil/findingId": f"f-{n}", "anvil/half": "sast", "anvil/confidence": 0.9,
            "anvil/verdict": verdict, "anvil/remediableByAgent": False,
            "anvil/reasoning": "installed version is inside the vulnerable range",
            "anvil/detector": {"kind": "host", "model": "matcher", "revision": "1"},
            "anvil/evidenceClass": "host", "anvil/trust": {"default": "verified"},
            "anvil/advisory": {"ids": ["DSA-5103-1"], "cveIds": list(cves), "sourceFeed": "osv",
                               "snapshotDigest": "d", "asOf": TS, "stalenessSeconds": 0,
                               "parseDegraded": False,
                               "excerpt": {"text": "BN_mod_sqrt() loops forever.", "trust": "untrusted"}},
            "anvil/risk": {"kevMember": kev, "kevRansomwareUse": False, "epssScore": 0.02},
        },
    }


def record(*results):
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
        "properties": {
            "anvil/schemaVersion": "1.0.0", "anvil/auditId": "a-1", "anvil/state": "sast_sealed",
            "anvil/version": 1, "anvil/createdAt": TS,
            "anvil/target": {"repoUrl": "https://example.invalid/r", "ref": "main", "commit": "abc",
                             "subpath": "", "provenance": "no_target_declared",
                             "provisioning": "ephemeral_manifest"},
            "anvil/trigger": {"kind": "manual", "policyId": "p", "policyRef": "r", "configSource": "c",
                              "actor": "a", "resolvedAt": TS},
            "anvil/deadline": {"deadlineAt": TS, "claimTimeoutSeconds": 60, "dastDeadlineSeconds": None},
            "anvil/index": {"counts": {"total": len(results), "sast": len(results), "dast": 0,
                                       "clusters": 0, "unclustered": len(results)},
                            "readOrder": [], "byCluster": {}, "byCwe": {}, "byPath": {},
                            "taskCards": "t", "blobs": "b"},
            "anvil/dastStatus": "skipped_no_manifest",
        },
        "runs": [{"tool": {"driver": {"name": "anvil"}},
                  "automationDetails": {"correlationGuid": "00000000-0000-4000-8000-000000000001"},
                  "results": list(results),
                  "properties": {"anvil/half": "sast", "anvil/status": "sealed", "anvil/sealedAt": TS,
                                 "anvil/advisorySnapshot": {"feedIds": ["osv"], "snapshotDigest": "d",
                                                            "scrapedAt": TS}}}],
    }


class AnvilImportTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="build-runner-04", agent_token="a" * 32,
                                        status=Host.Status.ONLINE)

    def test_fixture_is_a_valid_anvil_record(self):
        validate_record(record(_result(1)))  # the fixtures themselves must be honest

    def test_import_maps_onto_findings(self):
        counts = ingest_record(record(_result(1), _result(2, kev=True, cves=("CVE-2022-0779",)),
                                      _result(3, verdict="false_positive"),
                                      _result(4, host="repo-only")))
        self.assertEqual(counts, {"imported": 2, "false_positive": 1, "no_host": 1})
        f = VulnFinding.objects.get(plugin_id_or_oid="anvil:f-1")
        self.assertEqual((f.scanner, f.cve_id, f.severity), ("anvil", "CVE-2022-0778", "high"))
        self.assertEqual((f.package_name, f.installed_version), ("openssl", "1.1.1n-0+deb11u3"))
        self.assertEqual(f.advisory["confidence"], 0.9)
        self.assertEqual(f.description, "BN_mod_sqrt() loops forever.")
        self.assertEqual([e.kind for e in f.evidence.all()], ["package"])
        self.assertEqual(VulnFinding.objects.get(plugin_id_or_oid="anvil:f-2").severity, "critical")
        # Same package, same (unknown) fix: one fix group, one critical.
        self.assertEqual(VulnSummary.objects.get(host=self.host).critical, 1)

    def test_next_record_fixes_what_it_no_longer_carries(self):
        ingest_record(record(_result(1), _result(2)))
        ingest_record(record(_result(2)))
        self.assertEqual(VulnFinding.objects.get(plugin_id_or_oid="anvil:f-1").state, "fixed")

    def test_invalid_records_touch_nothing(self):
        ingest_record(record(_result(1)))
        bad = record(_result(2))
        bad["version"] = "2.2"
        broken = copy.deepcopy(record(_result(2)))
        broken["runs"][0]["results"][0]["partialFingerprints"]["anvilFindingId/v1"] = "abc"
        for doc in (bad, broken, {"runs": []}, [], "x"):
            with self.subTest(doc=str(doc)[:40]):
                with self.assertRaises(ScanIngestError):
                    ingest_record(doc)
        self.assertEqual(VulnFinding.objects.get().state, "open")

    def test_api_upload_with_a_pinned_host(self):
        other = Host.objects.create(hostname="other", agent_token="o" * 32, status=Host.Status.ONLINE)
        user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        client = APIClient()
        client.force_authenticate(user)
        resp = client.post(f"/api/v1/vulns/anvil/?host_id={other.id}",
                           {"record": record(_result(1, host="nowhere"))}, format="json")
        self.assertEqual(resp.json(), {"imported": 1, "false_positive": 0, "no_host": 0})
        self.assertEqual(VulnFinding.objects.get().host, other)
        resp = client.post("/api/v1/vulns/anvil/", {"record": {"version": "2.1.0"}}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("schema error", resp.json()["detail"])
