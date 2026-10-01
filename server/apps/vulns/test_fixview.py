"""M9 remediation by fix: groups across the fleet, and the task that fixes one."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.tasks.models import TaskDefinition

from .evidence import add_evidence
from .models import EpssScore, KevEntry, VulnFinding


def _host(name):
    return Host.objects.create(hostname=name, agent_token=f"{name}-t" * 8, status=Host.Status.ONLINE)


def _f(host, plugin, cve, sev="high", pkg="openssl", fixed="3.0.13-0ubuntu3.1", **extra):
    return VulnFinding.objects.create(host=host, scanner="vigil", plugin_id_or_oid=plugin, cve_id=cve,
                                      severity=sev, package_name=pkg, fixed_version=fixed,
                                      installed_version="3.0.13-0ubuntu3", **extra)


class FixGroupApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.a, self.b = _host("a"), _host("b")
        for host in (self.a, self.b):
            for n in range(3):
                f = _f(host, f"pkg:openssl:CVE-2024-000{n}", f"CVE-2024-000{n}",
                       sev="critical" if n == 0 else "medium",
                       advisory={"source": "dpkg"})
                add_evidence(f, "package", "dpkg:openssl", "openssl installed")
        add_evidence(VulnFinding.objects.filter(host=self.a).first(), "process", "1:nginx", "running")
        KevEntry.objects.create(cve_id="CVE-2024-0001", date_added="2024-01-01")
        EpssScore.objects.create(cve_id="CVE-2024-0002", epss=0.42, percentile=0.97)
        _f(self.a, "kb:KB5034441", "", sev="high", pkg="KB5034441", fixed="KB5034441")
        _f(self.b, "lib:CVE-2021-44228", "CVE-2021-44228", sev="critical",
           pkg="log4j-core", fixed="", affected_path="opt/app/log4j-core-2.14.1.jar",
           vendor_status="affected")

    def test_groups(self):
        rows = self.client.get("/api/v1/vulns/fix-groups/").json()["results"]
        by = {r["package"]: r for r in rows}
        ssl = by["openssl"]
        self.assertEqual((ssl["host_count"], ssl["finding_count"], ssl["severity"]), (2, 6, "critical"))
        self.assertEqual(ssl["cves"], ["CVE-2024-0000", "CVE-2024-0001", "CVE-2024-0002"])
        self.assertTrue(ssl["kev"])
        self.assertEqual(ssl["epss"], 0.42)
        self.assertTrue(ssl["running"])
        self.assertEqual({h["hostname"]: h["confidence"] for h in ssl["hosts"]},
                         {"a": "urgent", "b": "confirmed"})
        self.assertEqual(ssl["fix"]["kind"], "upgrade")
        self.assertEqual(by["KB5034441"]["fix"]["kind"], "kb")
        jar = by["log4j-core"]
        self.assertEqual((jar["fix"]["kind"], jar["vendor_status"]), ("none", "affected"))
        self.assertEqual(rows[0]["package"], "openssl", "KEV and running sort the critical first")

    def test_deploy_builds_the_fix_task_once(self):
        key = VulnFinding.objects.filter(package_name="openssl").first().fix_key
        body = self.client.post("/api/v1/vulns/fix-groups/deploy/", {"fix_key": key}, format="json").json()
        definition = TaskDefinition.objects.get(pk=body["definition_id"])
        self.assertEqual(sorted(body["host_ids"]), sorted([str(self.a.id), str(self.b.id)]))
        action = definition.parsed_spec["actions"][0]
        self.assertEqual((action["type"], action["params"]), ("app_upgrade", {"app": "openssl", "source": "dpkg"}))
        again = self.client.post("/api/v1/vulns/fix-groups/deploy/", {"fix_key": key}, format="json").json()
        self.assertEqual(again["definition_id"], body["definition_id"])
        kb = self.client.post("/api/v1/vulns/fix-groups/deploy/",
                              {"fix_key": VulnFinding.objects.get(package_name="KB5034441").fix_key},
                              format="json").json()
        kb_action = TaskDefinition.objects.get(pk=kb["definition_id"]).parsed_spec["actions"][0]
        self.assertEqual(kb_action["params"], {"include_kb": "KB5034441"})

    def test_no_fix_and_viewer_refusals(self):
        jar_key = VulnFinding.objects.get(package_name="log4j-core").fix_key
        resp = self.client.post("/api/v1/vulns/fix-groups/deploy/", {"fix_key": jar_key}, format="json")
        self.assertEqual(resp.status_code, 400)
        viewer = get_user_model().objects.create_user("v", password="x")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        client = APIClient()
        client.force_authenticate(viewer)
        self.assertEqual(client.get("/api/v1/vulns/fix-groups/").status_code, 200)
        self.assertEqual(client.post("/api/v1/vulns/fix-groups/deploy/", {"fix_key": "x"},
                                     format="json").status_code, 403)


class FixViewWiringTests(TestCase):
    def test_page_script_and_deploy_prefill(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        html = (root / "templates/pages/_vulns.html").read_text(encoding="utf-8")
        js = (root / "static/js/vigil-vulns.js").read_text(encoding="utf-8")
        deploy = (root / "static/js/vigil-deploy.js").read_text(encoding="utf-8")
        for element_id in ("vuln-fixes-section", "vuln-fixes", "vuln-fix-q"):
            self.assertIn(f'id="{element_id}"', html)
            self.assertIn(f"'{element_id}'", js)
        self.assertIn("/api/v1/vulns/fix-groups/", js)
        self.assertIn("openDeployModal(body.definition_id, { hostIds: body.host_ids })", js)
        self.assertIn("prefill.hostIds", deploy)


class DetectionAsFixTests(TestCase):
    """M10: a detection task naming a CVE is the fix for a group with none."""

    def setUp(self):
        from apps.tasks.spec import parse_and_validate
        from apps.tasks.test_detection_spec import LOG4SHELL
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        spec = parse_and_validate(LOG4SHELL)
        self.task = TaskDefinition.objects.create(name=spec["name"], owner=self.user,
                                                  yaml_source=LOG4SHELL, parsed_spec=spec)
        self.host = _host("app")
        self.jar = _f(self.host, "jar:CVE-2021-44228", "CVE-2021-44228", sev="critical",
                      pkg="log4j-core", fixed="", affected_path="opt/app/log4j-core-2.14.1.jar")

    def test_group_offers_the_detection_task_and_deploys_it(self):
        group = self.client.get("/api/v1/vulns/fix-groups/").json()["results"][0]
        self.assertEqual(group["fix"]["kind"], "detection")
        self.assertEqual(group["fix"]["definition_id"], str(self.task.id))
        body = self.client.post("/api/v1/vulns/fix-groups/deploy/", {"fix_key": self.jar.fix_key},
                                format="json").json()
        self.assertEqual(body, {"definition_id": str(self.task.id), "host_ids": [str(self.host.id)]})

    def test_archived_or_plain_tasks_do_not_count(self):
        from django.utils.timezone import now
        self.task.archived_at = now()
        self.task.save()
        group = self.client.get("/api/v1/vulns/fix-groups/").json()["results"][0]
        self.assertEqual(group["fix"]["kind"], "none")


class ContentSourceTests(TestCase):
    def test_default_and_serializer(self):
        from apps.tasks.serializers import TaskDefinitionSerializer
        definition = TaskDefinition.objects.create(name="t", yaml_source="", parsed_spec={})
        self.assertEqual(TaskDefinitionSerializer(definition).data["content_source"], "organization")
