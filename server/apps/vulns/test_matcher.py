"""M9 matcher: the inventory against local OSV data, with real version rules."""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils.timezone import now

from apps.hosts.models import Host, HostInventory
from apps.software.models import SoftwareItem, SoftwareSnapshot

from .matcher import _affected, host_ecosystems, match_host
from .models import OsvAffected, VulnFinding, VulnSummary
from .test_vulndata import OSV_OPENSSL
from .vulndata import import_osv_records


def _host(name="web", os_name="Ubuntu 24.04.1 LTS", os_version="24.04"):
    host = Host.objects.create(hostname=name, agent_token=f"{name}-tok" * 4,
                               status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)
    HostInventory.objects.create(host=host, os_name=os_name, os_version=os_version)
    SoftwareSnapshot.objects.create(host=host)
    return host


def _pkg(host, package_id, version, source="dpkg"):
    return SoftwareItem.objects.update_or_create(
        host=host, source=source, package_id=package_id, scope="machine", user="",
        defaults=dict(name=package_id, name_key=package_id, version=version, first_seen=now()))[0]


class VersionsCopyTests(SimpleTestCase):
    def test_server_copy_matches_the_agent(self):
        root = Path(settings.BASE_DIR).parent
        agent = (root / "agent/vigil_agent/versions.py").read_text(encoding="utf-8")
        server = (root / "server/apps/vulns/versions.py").read_text(encoding="utf-8")
        cut = server.index("\n", server.index("change both"))
        self.assertEqual(server[server.index("\n\n", cut):],
                         agent[agent.index("\n\n", agent.index("Three schemes") - 60):])


class EcosystemTests(TestCase):
    def test_mapping(self):
        cases = [
            (("Ubuntu 24.04.1 LTS", "24.04"), (["Ubuntu:24.04", "Ubuntu:24.04:LTS", "Ubuntu:Pro:24.04:LTS"], "deb")),
            (("Debian GNU/Linux 12 (bookworm)", "12"), (["Debian:12"], "deb")),
            (("Alpine Linux v3.20", "3.20.3"), (["Alpine:v3.20"], "generic")),
            (("Rocky Linux 9.4 (Blue Onyx)", "9.4"), (["Rocky Linux:9"], "rpm")),
            (("Arch Linux", ""), ([], "")),
        ]
        for n, ((name, version), expected) in enumerate(cases):
            with self.subTest(name=name):
                self.assertEqual(host_ecosystems(_host(f"h{n}", name, version)), expected)


class AffectedRangeTests(SimpleTestCase):
    def _row(self, events, versions=()):
        return OsvAffected(ranges=[{"type": "ECOSYSTEM", "events": events}], versions=list(versions))

    def test_ranges(self):
        fixed = self._row([{"introduced": "0"}, {"fixed": "3.0.13-0ubuntu3.1"}])
        self.assertTrue(_affected("3.0.13-0ubuntu3", fixed, "deb"))
        self.assertFalse(_affected("3.0.13-0ubuntu3.1", fixed, "deb"))
        self.assertFalse(_affected("3.0.13-0ubuntu3.4", fixed, "deb"))
        self.assertTrue(_affected("3.0.13~rc1-0ubuntu3.4", fixed, "deb"), "~ sorts first")
        window = self._row([{"introduced": "2.0"}, {"fixed": "2.5"}, {"introduced": "3.0"},
                            {"last_affected": "3.1"}])
        self.assertEqual([_affected(v, window, "generic") for v in ("1.9", "2.1", "2.5", "3.1", "3.2")],
                         [False, True, False, True, False])
        never_fixed = self._row([{"introduced": "1.0"}])
        self.assertTrue(_affected("9.9", never_fixed, "generic"))
        self.assertTrue(_affected("0.5", self._row([], versions=["0.5"]), "generic"))
        epoch = self._row([{"introduced": "0"}, {"fixed": "1:2.0-1"}])
        self.assertTrue(_affected("1:1.9-1", epoch, "rpm"))
        self.assertFalse(_affected("2:1.0-1", epoch, "rpm"), "a higher epoch wins")


class MatchHostTests(TestCase):
    def setUp(self):
        import_osv_records([OSV_OPENSSL])
        self.host = _host()

    def test_vulnerable_package_becomes_a_finding_with_evidence(self):
        _pkg(self.host, "openssl", "3.0.13-0ubuntu3")
        self.assertEqual(match_host(self.host), {"open": 1, "fixed": 0})
        f = VulnFinding.objects.get(host=self.host)
        self.assertEqual((f.scanner, f.cve_id, f.severity), ("vigil", "CVE-2024-0727", "medium"))
        self.assertEqual((f.installed_version, f.fixed_version, f.vendor_status),
                         ("3.0.13-0ubuntu3", "3.0.13-0ubuntu3.1", "fixed"))
        self.assertIn("PKCS12", f.description)
        self.assertEqual([e.kind for e in f.evidence.all()], ["package"])
        self.assertEqual(VulnSummary.objects.get(host=self.host).medium, 1)

    def test_upgrade_fixes_it_and_rematch_is_idempotent(self):
        _pkg(self.host, "openssl", "3.0.13-0ubuntu3")
        match_host(self.host)
        match_host(self.host)
        self.assertEqual(VulnFinding.objects.count(), 1)
        _pkg(self.host, "openssl", "3.0.13-0ubuntu3.1")
        self.assertEqual(match_host(self.host), {"open": 0, "fixed": 1})
        self.assertEqual(VulnFinding.objects.get().state, "fixed")

    def test_other_release_or_other_scanner_untouched(self):
        debian = _host("deb", "Debian GNU/Linux 12", "12")
        _pkg(debian, "openssl", "3.0.13-0ubuntu3")
        self.assertEqual(match_host(debian)["open"], 0, "an Ubuntu advisory is not Debian's")
        trivy = VulnFinding.objects.create(host=self.host, scanner="trivy", plugin_id_or_oid="x:CVE-1",
                                           cve_id="CVE-1", severity="high")
        match_host(self.host)
        trivy.refresh_from_db()
        self.assertEqual(trivy.state, "open", "the matcher only reconciles its own rows")

    def test_ingest_triggers_the_match(self):
        from apps.software.ingest import ingest_software
        from apps.software.test_software_ingest import digest_of, item, payload
        ingest_software(self.host, payload(digest_of("a"), [item("dpkg", "openssl", "openssl",
                                                                 "3.0.13-0ubuntu3")]))
        self.assertTrue(VulnFinding.objects.filter(host=self.host, scanner="vigil").exists())
