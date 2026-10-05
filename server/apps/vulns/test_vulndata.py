"""M9 vulnerability data: OSV, KEV and EPSS loaded from an offline bundle —
built here, in memory, so no test touches the network."""
import gzip
import io
import json
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile

from .cvss import cvss3_base_score, severity_for_score
from .models import EpssScore, KevEntry, OsvAdvisory, OsvAffected
from .vulndata import fetch_osv, import_bundle, import_osv_records

OSV_OPENSSL = {
    "id": "UBUNTU-CVE-2024-0727", "modified": "2024-03-01T00:00:00Z",
    "aliases": ["CVE-2024-0727"], "summary": "openssl PKCS12 NULL dereference",
    "details": "Processing a maliciously formatted PKCS12 file may lead OpenSSL to crash.",
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H"}],
    "affected": [{"package": {"ecosystem": "Ubuntu:24.04:LTS", "name": "openssl"},
                  "ranges": [{"type": "ECOSYSTEM", "events": [
                      {"introduced": "0"}, {"fixed": "3.0.13-0ubuntu3.1"}]}]}],
    "references": [{"type": "ADVISORY", "url": "https://ubuntu.com/security/CVE-2024-0727"}],
}
OSV_RATED = {
    "id": "DSA-5600-1", "aliases": ["CVE-2024-1111"],
    "database_specific": {"severity": "Important"},
    "affected": [{"package": {"ecosystem": "Debian:12", "name": "curl"},
                  "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}]}],
}
KEV = {"vulnerabilities": [{"cveID": "CVE-2024-0727", "dateAdded": "2024-03-02",
                            "dueDate": "2024-03-23", "vulnerabilityName": "OpenSSL",
                            "knownRansomwareCampaignUse": "Unknown"}]}
EPSS = "#model_version:v2023.03.01,score_date:2024-03-01T00:00:00+0000\ncve,epss,percentile\nCVE-2024-0727,0.00045,0.12\nCVE-2024-1111,0.9,0.99\n"


def _bundle_bytes() -> bytes:
    eco = io.BytesIO()
    with zipfile.ZipFile(eco, "w") as z:
        z.writestr("UBUNTU-CVE-2024-0727.json", json.dumps(OSV_OPENSSL))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("osv/Ubuntu/all.zip", eco.getvalue())
        z.writestr("osv/Debian/DSA-5600-1.json", json.dumps(OSV_RATED))
        z.writestr("kev.json", json.dumps(KEV))
        z.writestr("epss.csv.gz", gzip.compress(EPSS.encode()))
    return out.getvalue()


class CvssTests(SimpleTestCase):
    def test_known_scores(self):
        self.assertEqual(cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"), 10.0)
        self.assertEqual(cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), 9.8)
        self.assertEqual(cvss3_base_score("CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H"), 5.5)
        self.assertEqual(cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N"), 0.0)
        self.assertIsNone(cvss3_base_score("AV:N/AC:L/Au:N/C:P/I:P/A:P"))
        self.assertIsNone(cvss3_base_score("CVSS:3.1/AV:N"))
        self.assertEqual([severity_for_score(s) for s in (9.0, 7.0, 4.0, 0.1, 0)],
                         ["critical", "high", "medium", "low", "info"])


class BundleImportTests(TestCase):
    def _write(self, data: bytes, suffix=".zip") -> Path:
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(data)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_zip_bundle_loads_everything(self):
        counts = import_bundle(self._write(_bundle_bytes()))
        self.assertEqual(counts, {"osv": 2, "kev": 1, "epss": 2})
        adv = OsvAdvisory.objects.get(id="UBUNTU-CVE-2024-0727")
        self.assertEqual((adv.severity, adv.cvss_score), ("medium", 5.5))
        self.assertEqual(adv.cve_ids, ["CVE-2024-0727"])
        aff = adv.affected.get()
        self.assertEqual((aff.ecosystem, aff.package, aff.fixed),
                         ("Ubuntu:24.04:LTS", "openssl", "3.0.13-0ubuntu3.1"))
        rated = OsvAdvisory.objects.get(id="DSA-5600-1")
        self.assertEqual(rated.severity, "high", "the database's own rating, when there is no vector")
        self.assertEqual(rated.affected.get().fixed, "", "no fixed event: no fix")
        self.assertTrue(KevEntry.objects.filter(cve_id="CVE-2024-0727").exists())
        self.assertEqual(EpssScore.objects.get(cve_id="CVE-2024-1111").percentile, 0.99)

    def test_reimport_replaces_rather_than_duplicates(self):
        import_osv_records([OSV_OPENSSL])
        changed = {**OSV_OPENSSL, "affected": OSV_OPENSSL["affected"] * 2}
        import_osv_records([changed])
        self.assertEqual(OsvAffected.objects.count(), 2)
        import_osv_records([OSV_OPENSSL])
        self.assertEqual(OsvAffected.objects.count(), 1)

    def test_junk_is_skipped(self):
        self.assertEqual(import_osv_records([None, {}, {"id": "X", "affected": [{"package": {}}]}]), 1)
        with self.assertRaises(ValueError):
            import_bundle(self._write(b"not an archive", suffix=".bin"))

    def test_command_and_directory_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "osv").mkdir()
            (Path(d) / "osv" / "a.json").write_text(json.dumps(OSV_RATED))
            out = io.StringIO()
            call_command("import_vuln_data", d, stdout=out)
        self.assertIn("Loaded 1 OSV records", out.getvalue())

    def test_fetch_uses_the_bucket(self):
        eco = io.BytesIO()
        with zipfile.ZipFile(eco, "w") as z:
            z.writestr("x.json", json.dumps(OSV_OPENSSL))
        session = mock.Mock()
        session.get.return_value = mock.Mock(content=eco.getvalue(), raise_for_status=lambda: None)
        self.assertEqual(fetch_osv("Ubuntu", session=session), 1)
        self.assertIn("/Ubuntu/all.zip", session.get.call_args.args[0])


class DataEndpointTests(TestCase):
    def _client(self, role):
        user = get_user_model().objects.create_user(f"u-{role}", password="x")
        UserProfile.objects.create(user=user, role=role)
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_upload_and_status(self):
        admin = self._client(Role.ADMIN)
        upload = SimpleUploadedFile("vuln-data.zip", _bundle_bytes(), content_type="application/zip")
        resp = admin.post("/api/v1/vulns/data/", {"bundle": upload}, format="multipart")
        self.assertEqual(resp.json()["loaded"]["osv"], 2)
        status = admin.get("/api/v1/vulns/data/").json()
        self.assertEqual(status["osv_ecosystems"], ["Debian:12", "Ubuntu:24.04:LTS"])
        self.assertEqual(admin.post("/api/v1/vulns/data/", {}, format="multipart").status_code, 400)
        viewer = self._client(Role.VIEWER)
        self.assertEqual(viewer.get("/api/v1/vulns/data/").status_code, 403)
