"""Software ingest + read API.

The ingest contract is deliberately forgiving (a bad list must never break a
check-in) and deliberately replace-the-world with a digest short-circuit, so
the tests here pin both halves: what changes, and what must not.
"""
import hashlib
from datetime import timedelta
from unittest.mock import patch

from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from .ingest import ingest_software
from .models import SoftwareItem, SoftwareSnapshot, name_key


def digest_of(*ids):
    return hashlib.sha256("|".join(ids).encode()).hexdigest()


def item(source, package_id, name, version="", latest="", **extra):
    row = {"source": source, "id": package_id, "name": name, "version": version,
           "latest": latest, "scope": "machine", "user": "", "publisher": "",
           "managed": True}
    row.update(extra)
    return row


def payload(digest, items, collected_at="2026-09-29T12:00:00Z", errors=None):
    body = {"digest": digest, "collected_at": collected_at, "items": items}
    if errors is not None:
        body["errors"] = errors
    return body


class NameKeyTests(TestCase):
    def test_name_key_examples(self):
        for raw, expected in [
            ("Mozilla Firefox (x64 en-US)", "mozilla firefox"),
            ("7-Zip 23.01 (x64)", "7-zip"),
            ("Google Chrome", "google chrome"),
            ("openssl", "openssl"),
            ("Python 3.12.4 (64-bit)", "python"),
            # Must not be damaged:
            ("7-Zip", "7-zip"),
            ("Notepad++", "notepad++"),
            ("Microsoft Visual C++ 2015-2022 Redistributable",
             "microsoft visual c++ 2015-2022 redistributable"),
        ]:
            self.assertEqual(name_key(raw), expected, raw)


class IngestTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="ingest-host", agent_token="tok-ing",
                                        status=Host.Status.ONLINE)

    def test_checkin_stores_items_and_returns_digest(self):
        digest = digest_of("a", "b", "c")
        resp = self.client.post(
            "/api/v1/checkin",
            {"hostname": "ingest-host", "metrics": {},
             "software": payload(digest, [
                 item("dpkg", "openssl", "openssl", "3.0.13", "3.0.13.5"),
                 item("dpkg", "curl", "curl", "8.5.0"),
                 item("flatpak", "org.mozilla.firefox", "Mozilla Firefox (x64 en-US)",
                      "128.0"),
             ])},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-ing")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["software_digest"], digest)
        self.assertEqual(SoftwareItem.objects.filter(host=self.host).count(), 3)
        snap = SoftwareSnapshot.objects.get(host=self.host)
        self.assertEqual(snap.digest, digest)
        self.assertEqual(snap.item_count, 3)
        self.assertEqual(snap.collected_at.year, 2026)
        self.assertEqual(snap.errors, {})

        firefox = SoftwareItem.objects.get(host=self.host, package_id="org.mozilla.firefox")
        self.assertEqual(firefox.name_key, "mozilla firefox")
        self.assertEqual(firefox.latest_version, "")   # payload omitted "latest"

        # A later check-in with no `software` key leaves the list alone and
        # still reports the stored digest.
        again = self.client.post(
            "/api/v1/checkin", {"hostname": "ingest-host", "metrics": {}},
            content_type="application/json", HTTP_AUTHORIZATION="Bearer tok-ing")
        self.assertEqual(again.json()["software_digest"], digest)
        self.assertEqual(SoftwareItem.objects.filter(host=self.host).count(), 3)

    def test_same_digest_is_a_no_op(self):
        body = payload(digest_of("a"), [item("dpkg", "openssl", "openssl", "1.0")])
        first = ingest_software(self.host, body)
        item_pk = SoftwareItem.objects.get(host=self.host).pk
        before = SoftwareItem.objects.get(pk=item_pk).updated_at

        # Frozen future clock: "received_at moved" is then observable.
        later = now() + timedelta(minutes=5)
        with patch("apps.software.ingest.timezone.now", return_value=later):
            snap = ingest_software(self.host, body)
        self.assertEqual(snap.pk, first.pk)
        self.assertEqual(snap.item_count, 1)

        after = SoftwareItem.objects.get(pk=item_pk)
        self.assertEqual(after.updated_at, before, "same digest must not rewrite items")

        # The rewrite path is skipped outright, not merely made a no-op.
        with (
            patch.object(SoftwareItem.objects, "bulk_update",
                         side_effect=AssertionError("items rewritten on a known digest")),
            patch.object(SoftwareItem.objects, "bulk_create",
                         side_effect=AssertionError("items created on a known digest")),
        ):
            self.assertEqual(ingest_software(self.host, body).pk, first.pk)

    def test_replace_keeps_first_seen(self):
        original_seen = now() - timedelta(days=30)
        ingest_software(self.host, payload(digest_of("keep", "change", "drop"), [
            item("dpkg", "keep", "Keep App", "1.0"),
            item("dpkg", "change", "Change App", "1.0"),
            item("dpkg", "drop", "Drop App", "1.0"),
        ]))
        SoftwareItem.objects.filter(host=self.host).update(first_seen=original_seen)

        ingest_software(self.host, payload(digest_of("keep", "change", "new"), [
            item("dpkg", "keep", "Keep App", "1.0"),
            item("dpkg", "change", "Change App", "2.0"),
            item("rpm", "new", "New App", "3.0"),
        ]))
        self.assertEqual(SoftwareItem.objects.filter(host=self.host).count(), 3)
        self.assertFalse(SoftwareItem.objects.filter(host=self.host, package_id="drop").exists())

        keep = SoftwareItem.objects.get(host=self.host, package_id="keep")
        change = SoftwareItem.objects.get(host=self.host, package_id="change")
        new = SoftwareItem.objects.get(host=self.host, package_id="new")
        self.assertEqual(keep.first_seen, original_seen)
        self.assertEqual(change.first_seen, original_seen)
        self.assertEqual(change.version, "2.0")
        self.assertGreater(new.first_seen, original_seen)
        self.assertGreater(new.first_seen, SoftwareSnapshot.objects.get(host=self.host).received_at
                           - timedelta(seconds=5))

    def test_bad_items_are_skipped_not_fatal(self):
        digest = digest_of("good")
        snap = ingest_software(self.host, payload(digest, [
            item("naptime", "nightly", "Nightly Build"),      # unknown source
            item("dpkg", "", "No Id"),                        # empty id
            item("dpkg", "long", "L" * 400),                  # over-long name
            item("dpkg", "good", "Good App", "1.0"),
            "not-a-dict",
        ]))
        self.assertEqual(snap.item_count, 2)
        long_row = SoftwareItem.objects.get(host=self.host, package_id="long")
        self.assertEqual(len(long_row.name), 300)
        self.assertEqual(SoftwareItem.objects.filter(host=self.host, name="Good App").count(), 1)
        self.assertEqual(SoftwareItem.objects.filter(host=self.host).count(), 2)

        for bad in ["not-a-dict", {"digest": "xyz", "items": []},
                    {"digest": digest.upper(), "items": []},
                    {"digest": digest_of("good"), "items": "nope"}]:
            self.assertIsNone(ingest_software(self.host, bad), repr(bad))
            self.assertFalse(SoftwareItem.objects.filter(host=self.host, name="No Id").exists())
        self.assertEqual(SoftwareSnapshot.objects.get(host=self.host).digest, digest)

        resp = self.client.post(
            "/api/v1/checkin",
            {"hostname": "ingest-host", "metrics": {}, "software": {"digest": "nope"}},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer tok-ing")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["software_digest"], digest)

    def test_outdated_and_unmanaged(self):
        ingest_software(self.host, payload(digest_of("o1", "o2", "o3", "o4"), [
            item("dpkg", "o1", "Outdated App", "1.0", "2.0"),
            item("dpkg", "o2", "Current App", "1.0", "1.0"),
            item("dpkg", "o3", "Blind App", "1.0", ""),
            item("registry", "o4", "Loose App", "1.0", managed=False),
        ]))
        rows = {r.package_id: r for r in SoftwareItem.objects.filter(host=self.host)}
        self.assertTrue(rows["o1"].outdated)
        self.assertFalse(rows["o2"].outdated)
        self.assertFalse(rows["o3"].outdated)
        self.assertFalse(rows["o4"].managed)

    def test_errors_scope_and_duplicates_are_cleaned(self):
        snap = ingest_software(self.host, payload(digest_of("u1", "m1", "m2", "u3"), [
            item("winget", "user-app", "User App", "1.0", scope="per-user", user="bob"),
            item("winget", "dupe", "Machine App", scope="machine"),
            item("winget", "dupe", "Machine App Renamed", "9.9"),
            item("winget", "owned", "Owned App", "1.0", scope="user", user="bob"),
            item("winget", "bare", "Bare User App", "1.0", scope="user"),
        ], errors={"flatpak": "x" * 500, "snap": 42}))
        self.assertEqual(len(snap.errors["flatpak"]), 300)
        self.assertEqual(snap.errors["snap"], "42")
        rows = {(r.package_id, r.scope, r.user): r
                for r in SoftwareItem.objects.filter(host=self.host)}
        self.assertEqual(rows[("user-app", "machine", "bob")].scope, "machine")  # not a known scope
        self.assertEqual(rows[("dupe", "machine", "")].name, "Machine App Renamed")
        self.assertEqual(rows[("dupe", "machine", "")].version, "9.9")
        self.assertEqual(rows[("owned", "user", "bob")].scope, "user")
        self.assertEqual(rows[("bare", "user", "")].scope, "user")   # user may be empty
        self.assertEqual(snap.item_count, 4)

    def test_item_cap_keeps_the_first_max_items(self):
        with patch("apps.software.ingest.MAX_ITEMS", 2):
            snap = ingest_software(self.host, payload(digest_of("c1", "c2", "c3"), [
                item("dpkg", "c1", "Cap App One", "1.0"),
                item("dpkg", "c2", "Cap App Two", "1.0"),
                item("dpkg", "c3", "Cap App Three", "1.0"),
            ]))
        self.assertEqual(snap.item_count, 2)
        self.assertEqual(snap.item_count, SoftwareItem.objects.filter(host=self.host).count())
        self.assertFalse(SoftwareItem.objects.filter(host=self.host, package_id="c3").exists())

    def test_ingest_never_raises_to_the_checkin(self):
        digest = digest_of("boom")
        ingest_software(self.host, payload(digest, [item("dpkg", "keep", "Keep App", "1.0")]))
        with patch("apps.software.ingest.SoftwareItem.objects.bulk_create",
                   side_effect=RuntimeError("db exploded")):
            self.assertIsNone(ingest_software(
                self.host, payload(digest_of("boom2"),
                                   [item("dpkg", "other", "Other App", "2.0")])))
        resp = self.client.post(
            "/api/v1/checkin",
            {"hostname": "ingest-host", "metrics": {},
             "software": payload(digest, [item("dpkg", "keep", "Keep App", "1.0")])},
            content_type="application/json", HTTP_AUTHORIZATION="Bearer tok-ing")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["software_digest"], digest)
        self.assertEqual(SoftwareItem.objects.filter(host=self.host).count(), 1)


class ReadApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("soft-admin", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.h1 = Host.objects.create(hostname="alpha", agent_token="tok-a",
                                      status=Host.Status.ONLINE)
        self.h2 = Host.objects.create(hostname="beta", agent_token="tok-b",
                                      status=Host.Status.ONLINE)
        self.h3 = Host.objects.create(hostname="gamma", agent_token="tok-g",
                                      status=Host.Status.ONLINE)

        ingest_software(self.h1, payload(digest_of("ff1"), [
            item("winget", "Mozilla.Firefox", "Mozilla Firefox (x64 en-US)", "128.0", "129.0"),
        ]))
        ingest_software(self.h2, payload(digest_of("ff2"), [
            item("winget", "Mozilla.Firefox", "Mozilla Firefox (x64 en-US)", "129.0", "129.0"),
        ]))
        ingest_software(self.h3, payload(digest_of("zip"), [
            item("winget", "7zip.7zip", "7-Zip 23.01 (x64)", "23.01", managed=False),
        ]))

    def test_apps_endpoint_groups_and_filters(self):
        body = self.client.get("/api/v1/software/apps/").json()
        self.assertEqual(body["count"], 2)
        firefox = next(r for r in body["results"] if r["name_key"] == "mozilla firefox")
        self.assertEqual(firefox["hosts"], 2)
        self.assertEqual(firefox["name"], "Mozilla Firefox (x64 en-US)")
        self.assertEqual(firefox["sources"], ["winget"])
        self.assertEqual(firefox["versions"], {"128.0": 1, "129.0": 1})
        self.assertEqual(firefox["outdated"], 1)
        self.assertEqual(firefox["unmanaged"], 0)
        sevenzip = next(r for r in body["results"] if r["name_key"] == "7-zip")
        self.assertEqual(sevenzip["hosts"], 1)
        self.assertEqual(sevenzip["outdated"], 0)
        self.assertEqual(sevenzip["unmanaged"], 1)
        self.assertEqual(body["results"][0]["name_key"], "mozilla firefox")

        unmanaged = self.client.get("/api/v1/software/apps/?unmanaged=1").json()
        self.assertEqual([r["name_key"] for r in unmanaged["results"]], ["7-zip"])

        q = self.client.get("/api/v1/software/apps/?q=fire").json()
        self.assertEqual([r["name_key"] for r in q["results"]], ["mozilla firefox"])

        outdated = self.client.get("/api/v1/software/apps/?outdated=1").json()
        self.assertEqual([r["name_key"] for r in outdated["results"]], ["mozilla firefox"])

        rpm = self.client.get("/api/v1/software/apps/?source=rpm").json()
        self.assertEqual(rpm["count"], 0)
        winget = self.client.get("/api/v1/software/apps/?source=winget").json()
        self.assertEqual(winget["count"], 2)

    def test_app_detail_lists_rows_sorted_by_hostname(self):
        body = self.client.get("/api/v1/software/apps/mozilla firefox/").json()
        self.assertEqual(body["name_key"], "mozilla firefox")
        self.assertEqual([r["hostname"] for r in body["rows"]], ["alpha", "beta"])
        row = body["rows"][0]
        self.assertEqual(sorted(row.keys()), sorted([
            "host_id", "hostname", "source", "package_id", "name", "version",
            "latest_version", "outdated", "scope", "user", "managed", "first_seen"]))
        self.assertTrue(row["outdated"])
        self.assertEqual(row["package_id"], "Mozilla.Firefox")
        self.assertEqual(row["name"], "Mozilla Firefox (x64 en-US)")
        self.assertEqual(row["scope"], "machine")
        self.assertEqual(row["user"], "")

    def test_host_endpoint_returns_snapshot_and_items(self):
        body = self.client.get(f"/api/v1/software/hosts/{self.h1.id}/").json()
        self.assertEqual(body["snapshot"]["digest"], digest_of("ff1"))
        self.assertEqual(body["snapshot"]["item_count"], 1)
        self.assertEqual(body["snapshot"]["errors"], {})
        row = body["items"][0]
        self.assertEqual(sorted(row.keys()), sorted([
            "name", "source", "package_id", "version", "latest_version",
            "outdated", "scope", "user", "managed", "first_seen", "publisher"]))
        self.assertTrue(row["outdated"])
        self.assertEqual(row["name"], "Mozilla Firefox (x64 en-US)")

        unknown = self.client.get(
            f"/api/v1/software/hosts/{Host.objects.create(hostname='n', agent_token='t-n').id}/"
        ).json()
        self.assertIsNone(unknown["snapshot"])
        self.assertEqual(unknown["items"], [])

    def test_host_endpoint_is_scoped(self):
        west = Site.objects.create(name="West Campus", slug="west-campus")
        HostSiteAssignment.objects.create(host=self.h1, site=west)
        dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=dana, site=west, role=Role.ADMIN)
        scoped = APIClient()
        scoped.force_authenticate(user=dana)

        denied = scoped.get(f"/api/v1/software/hosts/{self.h2.id}/")
        control = scoped.get(f"/api/v1/hosts/{self.h2.id}/")
        self.assertEqual(denied.status_code, 404, denied.content)
        self.assertEqual(denied.status_code, control.status_code)
        self.assertEqual(denied.json(), control.json())

        ok = scoped.get(f"/api/v1/software/hosts/{self.h1.id}/")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()["snapshot"]["item_count"], 1)

        apps_body = scoped.get("/api/v1/software/apps/").json()
        self.assertEqual([r["name_key"] for r in apps_body["results"]], ["mozilla firefox"])
        self.assertEqual(apps_body["results"][0]["hosts"], 1)

        detail = scoped.get("/api/v1/software/apps/mozilla firefox/").json()
        self.assertEqual([r["hostname"] for r in detail["rows"]], ["alpha"])

    def test_rejected_hosts_are_excluded(self):
        self.h2.status = Host.Status.REJECTED
        self.h2.save(update_fields=["status"])
        body = self.client.get("/api/v1/software/apps/").json()
        firefox = next(r for r in body["results"] if r["name_key"] == "mozilla firefox")
        self.assertEqual(firefox["hosts"], 1)
        self.assertEqual(firefox["versions"], {"128.0": 1})

    def test_pagination_counts_and_slices(self):
        body = self.client.get("/api/v1/software/apps/?limit=1").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["name_key"], "mozilla firefox")
        page2 = self.client.get("/api/v1/software/apps/?limit=1&offset=1").json()
        self.assertEqual([r["name_key"] for r in page2["results"]], ["7-zip"])
        self.assertEqual(self.client.get("/api/v1/software/apps/?limit=abc").json()["count"], 2)


class EndpointAuthTests(TestCase):
    def test_endpoints_require_authentication(self):
        client = APIClient()
        host = Host.objects.create(hostname="anon", agent_token="tok-anon")
        for url in ("/api/v1/software/apps/", "/api/v1/software/apps/openssl/",
                    f"/api/v1/software/hosts/{host.id}/"):
            response = client.get(url)
            self.assertIn(response.status_code, (401, 403), url)

    def test_checkin_without_software_key_reports_empty_digest(self):
        host = Host.objects.create(hostname="quiet", agent_token="tok-quiet",
                                   status=Host.Status.ONLINE)
        resp = self.client.post("/api/v1/checkin",
                                {"hostname": "quiet", "metrics": {}},
                                content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer tok-quiet")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["software_digest"], "")
        self.assertFalse(SoftwareSnapshot.objects.filter(host=host).exists())
