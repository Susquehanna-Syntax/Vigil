"""M9 fix groups: the score counts what fixes things, not how many CVEs."""
import importlib
from datetime import timedelta

from django.apps import apps as django_apps
from django.test import SimpleTestCase, TestCase
from django.utils.timezone import localdate

from apps.hosts.models import Host

from .fixgroups import fix_key_for
from .models import VulnFinding, VulnSummary
from .scoring import recompute_summary


def _f(host, plugin, cve="", sev="critical", pkg="", fixed="", scanner="trivy", path="", due=None):
    f = VulnFinding.objects.create(host=host, scanner=scanner, plugin_id_or_oid=plugin, cve_id=cve,
                                   severity=sev, package_name=pkg, fixed_version=fixed,
                                   affected_path=path)
    if due is not None:
        VulnFinding.objects.filter(pk=f.pk).update(due_date=due)
    return f


class FixKeyTests(SimpleTestCase):
    def test_keys(self):
        self.assertEqual(fix_key_for("OpenSSL", "3.0.13", "trivy", "x"), "openssl@3.0.13")
        self.assertEqual(fix_key_for("openssl", "", "vigil", "x"), "openssl@nofix")
        self.assertEqual(fix_key_for("log4j-core", "2.17.1", "trivy", "x", "opt/a.jar"),
                         "log4j-core@2.17.1|opt/a.jar")
        self.assertEqual(fix_key_for("", "", "nessus", "19506"), "nessus:19506")


class FixGroupScoringTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="h", agent_token="k" * 32, status=Host.Status.ONLINE)

    def test_one_package_with_a_hundred_cves_is_one_critical(self):
        for n in range(100):
            _f(self.host, f"openssl:CVE-{n}", cve=f"CVE-2024-{n:04d}", pkg="openssl", fixed="3.0.13",
               sev="critical" if n == 0 else "medium")
        summary = recompute_summary(self.host)
        self.assertEqual((summary.critical, summary.medium), (1, 0))

    def test_different_fixes_are_different_groups(self):
        _f(self.host, "a", cve="CVE-1", pkg="openssl", fixed="3.0.13")
        _f(self.host, "b", cve="CVE-2", pkg="openssl", fixed="3.0.14", sev="high")
        _f(self.host, "c", cve="CVE-3", pkg="curl", fixed="8.5", sev="high")
        summary = recompute_summary(self.host)
        self.assertEqual((summary.critical, summary.high), (1, 2))

    def test_same_cve_from_two_scanners_still_counts_once(self):
        _f(self.host, "19506", cve="CVE-2024-9", scanner="nessus", sev="high")
        _f(self.host, "openssl:CVE-2024-9", cve="CVE-2024-9", pkg="openssl", fixed="3.0.13")
        summary = recompute_summary(self.host)
        self.assertEqual((summary.critical, summary.high), (1, 0))

    def test_group_is_due_at_its_earliest_cve(self):
        soon = localdate() - timedelta(days=3)
        _f(self.host, "a", cve="CVE-1", pkg="openssl", fixed="3.0.13", sev="critical",
           due=localdate() + timedelta(days=30))
        _f(self.host, "b", cve="CVE-2", pkg="openssl", fixed="3.0.13", sev="low", due=soon)
        summary = recompute_summary(self.host)
        self.assertEqual((summary.critical, summary.low, summary.overdue_count), (1, 0, 1))

    def test_fix_key_follows_the_finding(self):
        f = _f(self.host, "a", pkg="curl", fixed="8.5")
        self.assertEqual(f.fix_key, "curl@8.5")
        f.fixed_version = "8.6"
        f.save(update_fields=["fixed_version"])
        f.refresh_from_db()
        self.assertEqual(f.fix_key, "curl@8.6")

    def test_migration_rescores_existing_installs(self):
        for n in range(5):
            _f(self.host, f"p{n}", cve=f"CVE-{n}", pkg="openssl", fixed="3.0.13")
        VulnFinding.objects.update(fix_key="")
        VulnSummary.objects.create(host=self.host, critical=5, score=50)
        migration = importlib.import_module("apps.vulns.migrations.0016_fix_groups_rescore")
        migration._forward(django_apps, None)
        self.assertEqual(set(VulnFinding.objects.values_list("fix_key", flat=True)), {"openssl@3.0.13"})
        summary = VulnSummary.objects.get(host=self.host)
        self.assertEqual(summary.critical, 1)
        self.assertEqual(summary.score, recompute_summary(self.host).score)
