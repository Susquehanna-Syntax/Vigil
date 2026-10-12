"""The Store's catalog (QA-26): parsers on real-shaped samples, the sync's
upsert, the fetch rules, the API and the install task."""
import gzip
import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

import yaml
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils.timezone import now
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host, HostInventory

from . import sync
from .download import FetchError, fetch
from .models import CatalogPackage, CatalogSync
from .sources import apt, flathub, winget

FLATHUB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<components version="0.8" origin="flatpak">
  <component type="desktop-application">
    <id>org.mozilla.firefox</id>
    <name>Firefox</name>
    <name xml:lang="de">Feuerfuchs</name>
    <summary>Fast, Private &amp; Safe Web Browser</summary>
    <developer_name>Mozilla</developer_name>
    <description><p>When it comes to your life online.</p><p xml:lang="de">Wenn es</p><ul><li>Private</li></ul></description>
    <icon height="64" type="cached" width="64">org.mozilla.firefox.png</icon>
    <icon height="128" type="cached" width="128">org.mozilla.firefox.png</icon>
    <categories><category>Network</category><category>WebBrowser</category></categories>
    <project_license>MPL-2.0</project_license>
    <url type="homepage">https://www.mozilla.org/firefox/</url>
    <releases><release version="157.0.1" timestamp="1"/></releases>
  </component>
  <component type="runtime"><id>org.freedesktop.Platform</id><name>Freedesktop Platform</name></component>
</components>"""

APT_PACKAGES = """Package: nginx
Version: 1.24.0-2ubuntu7
Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
Section: web
Homepage: https://nginx.org
Description: small, powerful, scalable web/proxy server
 Nginx ("engine X") is a high-performance web and reverse proxy server.
 .
 It was written by Igor Sysoev.

Package: libssl3t64
Version: 3.0.13
Section: libs
Description: Secure Sockets Layer toolkit - shared libraries

Package: firefox-locale-de
Version: 1:1snap1
Section: web
Description: German language pack for Firefox

Package: nginx-dev
Version: 1.24.0
Section: devel
Description: nginx web/proxy server (development files)
"""


def _winget_msix() -> bytes:
    fd, path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE packages(rowid INTEGER PRIMARY KEY, id TEXT, name TEXT, moniker TEXT,
                              latest_version TEXT, arp_min_version TEXT, arp_max_version TEXT, hash BLOB);
        CREATE TABLE norm_publishers2(norm_publisher TEXT, package INTEGER);
        CREATE TABLE tags2(rowid INTEGER PRIMARY KEY, tag TEXT);
        CREATE TABLE tags2_map(tag INTEGER, package INTEGER);
        INSERT INTO packages VALUES (1, 'Mozilla.Firefox', 'Mozilla Firefox (en-US)', 'firefox', '157.0.1', NULL, NULL, NULL);
        INSERT INTO packages VALUES (2, 'VideoLAN.VLC', 'VLC media player', 'vlc', '3.0.24', NULL, NULL, NULL);
        INSERT INTO packages VALUES (3, 'Mozilla.Firefox.de', 'Mozilla Firefox (de)', NULL, '157.0.1', NULL, NULL, NULL);
        INSERT INTO norm_publishers2 VALUES ('mozilla', 1);
        INSERT INTO tags2 VALUES (1, 'browser'), (2, 'foss');
        INSERT INTO tags2_map VALUES (1, 1), (2, 1), (2, 2);
    """)
    conn.commit()
    conn.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(path, "Public/index.db")
        z.writestr("AppxManifest.xml", "<x/>")
    Path(path).unlink()
    return buf.getvalue()


class ParserTests(SimpleTestCase):
    def test_flathub_reads_apps_untranslated_with_icons(self):
        [ff] = list(flathub.parse(gzip.compress(FLATHUB_XML.encode())))
        self.assertEqual((ff["package_id"], ff["name"], ff["publisher"], ff["version"], ff["license"]),
                         ("org.mozilla.firefox", "Firefox", "Mozilla", "157.0.1", "MPL-2.0"))
        self.assertEqual(ff["summary"], "Fast, Private & Safe Web Browser")
        self.assertEqual(ff["description"], "When it comes to your life online.\n- Private")
        self.assertEqual(ff["categories"], ["Network", "WebBrowser"])
        self.assertEqual(ff["icon_url"], flathub.ICON_BASE + "org.mozilla.firefox.png")

    def test_winget_reads_the_index_inside_the_msix(self):
        rows = {r["package_id"]: r for r in winget.parse(_winget_msix())}
        self.assertEqual(rows["Mozilla.Firefox"]["version"], "157.0.1")
        self.assertEqual(rows["Mozilla.Firefox"]["name"], "Mozilla Firefox", "the language suffix goes")
        self.assertNotIn("Mozilla.Firefox.de", rows, "a per-language copy beside the base package goes")
        self.assertEqual(rows["Mozilla.Firefox"]["publisher"], "Mozilla")
        self.assertEqual(rows["Mozilla.Firefox"]["categories"], ["browser", "foss"])
        self.assertEqual(rows["VideoLAN.VLC"]["categories"], ["foss"])

    def test_apt_skips_libraries_and_dev_packages(self):
        rows = list(apt.parse(gzip.compress(APT_PACKAGES.encode())))
        self.assertEqual([r["package_id"] for r in rows], ["nginx"])
        nginx = rows[0]
        self.assertEqual(nginx["summary"], "small, powerful, scalable web/proxy server")
        self.assertTrue(nginx["description"].startswith('Nginx ("engine X")'))
        self.assertIn("\n\nIt was written", nginx["description"])
        self.assertEqual(nginx["publisher"], "Ubuntu Developers")

    def test_apt_release_for_inventory(self):
        self.assertEqual(apt.release_for("Ubuntu", "24.04.1"), "ubuntu:noble")
        self.assertEqual(apt.release_for("Debian GNU/Linux", "12"), "debian:bookworm")
        self.assertEqual(apt.release_for("Fedora Linux", "40"), "")
        self.assertTrue(apt.urls("ubuntu:noble", "arm64")[0].startswith("https://ports.ubuntu.com/"))


class FetchRuleTests(SimpleTestCase):
    def test_only_allowlisted_hosts(self):
        with self.assertRaises(FetchError):
            fetch("https://example.com/index.gz")
        with self.assertRaises(FetchError):
            fetch("http://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz")   # HTTPS only

    def test_redirects_are_not_followed_and_size_is_capped(self):
        class Resp:
            def __init__(self, status, body=b"", headers=None):
                self.status_code, self.headers, self._body = status, headers or {}, body

            def iter_content(self, chunk_size):
                yield self._body

            def close(self):
                pass

        with mock.patch("requests.get", return_value=Resp(302, headers={"Location": "http://169.254.169.254/"})) as get:
            with self.assertRaises(FetchError):
                fetch("https://dl.flathub.org/x")
            self.assertIs(get.call_args.kwargs["allow_redirects"], False)
        with mock.patch("requests.get", return_value=Resp(200, b"x" * 2000)):
            with self.assertRaises(FetchError):
                fetch("https://dl.flathub.org/x", max_bytes=1000)


class _FreshSettings:
    """Instance settings are cached in memory; a test's write must not leak."""

    def setUp(self):
        from apps.instance import config
        config.invalidate()
        super().setUp()

    def tearDown(self):
        from apps.instance import config
        config.invalidate()
        super().tearDown()


class SyncTests(_FreshSettings, TestCase):
    def test_wanted_follows_the_fleet(self):
        win = Host.objects.create(hostname="w", agent_token="t1", status=Host.Status.ONLINE, os="Windows 11")
        ub = Host.objects.create(hostname="u", agent_token="t2", status=Host.Status.ONLINE, os="Ubuntu 24.04")
        HostInventory.objects.create(host=ub, os_name="Ubuntu", os_version="24.04", architecture="x86_64")
        Host.objects.create(hostname="r", agent_token="t3", status="rejected", os="Debian 12")
        self.assertTrue(win)
        got = [(m.SOURCE, rel) for m, rel, _urls in sync.wanted()]
        self.assertEqual(got, [("winget", ""), ("flatpak", ""), ("dpkg", "ubuntu:noble")])

    def test_store_adds_updates_and_removes(self):
        CatalogPackage.objects.create(source="flatpak", package_id="gone", name="Gone", platform="linux")
        CatalogPackage.objects.create(source="flatpak", package_id="org.a", name="A", version="1",
                                      platform="linux")
        n = sync.store(flathub, "", [{"package_id": "org.a", "name": "A", "version": "2"},
                                     {"package_id": "org.b", "name": "B"}])
        self.assertEqual(n, 2)
        self.assertEqual(dict(CatalogPackage.objects.values_list("package_id", "version")),
                         {"org.a": "2", "org.b": ""})

    def test_a_failed_fetch_is_recorded_and_keeps_the_rows(self):
        CatalogPackage.objects.create(source="winget", package_id="Keep.Me", name="Keep", platform="windows")
        with mock.patch.object(sync, "fetch", side_effect=FetchError("cdn down")):
            row = sync.sync_one(winget, "", [winget.URL])
        self.assertEqual(row.error, "cdn down")
        self.assertTrue(CatalogPackage.objects.filter(package_id="Keep.Me").exists())

    @override_settings(VIGIL_CATALOG_SYNC=False)
    def test_the_daily_task_does_nothing_while_off(self):
        from .tasks import sync_catalog
        with mock.patch.object(sync, "sync_all") as run:
            self.assertEqual(sync_catalog(), "catalog sync is off")
        run.assert_not_called()


def _user(role):
    u = get_user_model().objects.create_user(f"u-{role}", password="x")
    UserProfile.objects.create(user=u, role=role)
    c = APIClient()
    c.force_authenticate(u)
    return u, c


class ApiTests(_FreshSettings, TestCase):
    def setUp(self):
        super().setUp()
        from apps.software.models import SoftwareItem
        self.ff_flat = CatalogPackage.objects.create(
            source="flatpak", package_id="org.mozilla.firefox", name="Firefox", platform="linux",
            summary="Web browser", icon_url=flathub.ICON_BASE + "org.mozilla.firefox.png",
            categories=["Network"])
        self.ff_win = CatalogPackage.objects.create(
            source="winget", package_id="Mozilla.Firefox", name="Mozilla Firefox", platform="windows")
        self.ff_flat.publisher = "Mozilla"
        self.ff_flat.save()
        for rel in ("ubuntu:jammy", "ubuntu:noble"):
            CatalogPackage.objects.create(source="dpkg", release=rel, package_id="nginx", name="nginx",
                                          platform="linux", version=rel)
        host = Host.objects.create(hostname="w", agent_token="t1", status=Host.Status.ONLINE)
        SoftwareItem.objects.create(host=host, source="winget", package_id="Mozilla.Firefox",
                                    name="Firefox", name_key="firefox", first_seen=now())
        self.admin, self.client_admin = _user(Role.ADMIN)

    def test_browse_one_card_per_package_with_installed_counts(self):
        body = self.client_admin.get("/api/v1/catalog/").json()
        self.assertEqual(body["count"], 3, "nginx in two releases is one card")
        cards = {(c["source"], c["package_id"]): c for c in body["results"]}
        self.assertEqual(cards[("winget", "Mozilla.Firefox")]["installed"], 1)
        self.assertTrue(cards[("flatpak", "org.mozilla.firefox")]["icon"])
        hits = self.client_admin.get("/api/v1/catalog/", {"q": "fire", "platform": "windows"}).json()
        self.assertEqual([c["package_id"] for c in hits["results"]], ["Mozilla.Firefox"])
        cat = self.client_admin.get("/api/v1/catalog/", {"category": "Network"}).json()
        self.assertEqual([c["package_id"] for c in cat["results"]], ["org.mozilla.firefox"])

    def test_detail_lists_releases_and_the_same_app_elsewhere(self):
        body = self.client_admin.get(f"/api/v1/catalog/{self.ff_flat.id}/").json()
        self.assertEqual([(a["source"], a["installed"]) for a in body["also"]], [("winget", 1)])
        nginx = CatalogPackage.objects.filter(package_id="nginx").first()
        body = self.client_admin.get(f"/api/v1/catalog/{nginx.id}/").json()
        self.assertEqual([r["release"] for r in body["releases"]], ["ubuntu:jammy", "ubuntu:noble"])

    def test_install_task_guards_each_manager_and_is_reused(self):
        resp = self.client_admin.post("/api/v1/catalog/install-task/",
                                      {"packages": [self.ff_win.id, self.ff_flat.id]}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        from apps.tasks.models import TaskDefinition
        d = TaskDefinition.objects.get(pk=resp.json()["definition_id"])
        steps = yaml.safe_load(d.yaml_source)["actions"]
        self.assertEqual([(s["params"]["source"], s["when"]) for s in steps],
                         [("winget", 'agent.os == "windows"'), ("flatpak", 'agent.os == "linux"')])
        again = self.client_admin.post("/api/v1/catalog/install-task/",
                                       {"packages": [self.ff_win.id]}, format="json").json()
        self.assertEqual(again["definition_id"], str(d.id))
        self.assertEqual(TaskDefinition.objects.filter(name="Install Mozilla Firefox").count(), 1)

    def test_turning_it_on_is_for_admins(self):
        _viewer, client_viewer = _user(Role.VIEWER)
        self.assertEqual(client_viewer.post("/api/v1/catalog/enable/", {}, format="json").status_code, 403)
        with mock.patch("apps.catalog.views._sync_in_background") as kick:
            body = self.client_admin.post("/api/v1/catalog/enable/", {}, format="json").json()
        self.assertTrue(body["enabled"])
        kick.assert_called_once()

    def test_search_ranks_name_matches_before_summary_matches(self):
        CatalogPackage.objects.bulk_create([
            CatalogPackage(source="dpkg", package_id=f"lib-x-{n}", name=f"lib-x-{n}",
                           summary="tool for pycharm users", platform="linux")
            for n in range(600)])
        CatalogPackage.objects.create(source="flatpak", package_id="com.jetbrains.PyCharm-Community",
                                      name="PyCharm Community", platform="linux")
        body = self.client_admin.get("/api/v1/catalog/", {"q": "pycharm"}).json()
        self.assertEqual(body["count"], 601)
        self.assertEqual(body["results"][0]["package_id"], "com.jetbrains.PyCharm-Community",
                         "the app you typed, not the first summary match alphabetically")

    def test_search_words_any_order_and_case(self):
        CatalogPackage.objects.create(source="flatpak", package_id="com.jetbrains.PyCharm-Community",
                                      name="PyCharm Community Edition", platform="linux")
        CatalogPackage.objects.create(source="dpkg", package_id="pycharm", name="pycharm",
                                      platform="linux", summary="Community edition")
        for q in ("community PYCHARM", "pycharm-community"):
            hits = self.client_admin.get("/api/v1/catalog/", {"q": q}).json()
            self.assertEqual([c["package_id"] for c in hits["results"]],
                             ["com.jetbrains.PyCharm-Community", "pycharm"], q)

    def test_status_lists_wanted_sources(self):
        Host.objects.filter(pk=Host.objects.first().pk).update(os="Windows 11")
        wanted = self.client_admin.get("/api/v1/catalog/status/").json()["wanted"]
        self.assertIn(("winget", "windows"), {(w["source"], w["platform"]) for w in wanted})

    def test_sync_now_is_admin_only_and_needs_the_catalog_on(self):
        _viewer, client_viewer = _user(Role.VIEWER)
        self.assertEqual(client_viewer.post("/api/v1/catalog/sync/", {}, format="json").status_code, 403)
        self.assertEqual(self.client_admin.post("/api/v1/catalog/sync/", {}, format="json").status_code, 409)
        from apps.instance import config
        config.write("VIGIL_CATALOG_SYNC", "true", user=self.admin)
        with mock.patch("apps.catalog.views._sync_in_background") as kick:
            resp = self.client_admin.post("/api/v1/catalog/sync/", {}, format="json")
        self.assertEqual(resp.status_code, 202)
        kick.assert_called_once()

    def test_icons_are_not_fetched_while_the_catalog_is_off(self):
        with mock.patch("apps.catalog.views.fetch") as get:
            resp = self.client_admin.get(f"/api/v1/catalog/{self.ff_flat.id}/icon/")
        self.assertEqual(resp.status_code, 404)
        get.assert_not_called()

    def test_an_icon_must_be_a_png(self):
        from apps.instance import config
        config.write("VIGIL_CATALOG_SYNC", "true")
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            with mock.patch("apps.catalog.views.fetch", return_value=b"<svg onload=alert(1)>"):
                self.assertEqual(self.client_admin.get(f"/api/v1/catalog/{self.ff_flat.id}/icon/").status_code, 404)
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
            with mock.patch("apps.catalog.views.fetch", return_value=png) as get:
                resp = self.client_admin.get(f"/api/v1/catalog/{self.ff_flat.id}/icon/")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(b"".join(resp.streaming_content), png)
                self.client_admin.get(f"/api/v1/catalog/{self.ff_flat.id}/icon/")
            self.assertEqual(get.call_count, 1, "cached after the first fetch")
        self.assertFalse(CatalogSync.objects.exists())


class StorePageWiringTests(SimpleTestCase):
    def test_page_sidebar_and_script(self):
        root = Path(__file__).resolve().parents[2]
        base = (root / "templates/base.html").read_text(encoding="utf-8")
        dash = (root / "templates/dashboard.html").read_text(encoding="utf-8")
        js = (root / "static/js/vigil-store.js").read_text(encoding="utf-8")
        self.assertIn('data-page="store"', base)
        self.assertIn("js/vigil-store.js", base)
        self.assertIn('{% include "pages/_store.html" %}', dash)
        for needle in ("/api/v1/catalog/install-task/", "openDeployModal(res.definition_id)",
                       "policyFromApp(storeState.current.name", "escHtml(c.name)", "data-store-enable"):
            self.assertIn(needle, js)
        self.assertNotIn("onerror=", js, "CSP: no inline handlers; icons fall back in _wireIcons")


class DecompressionCapTests(SimpleTestCase):
    def test_a_gzip_bomb_is_refused_not_inflated(self):
        from . import download
        bomb = gzip.compress(b"\0" * (4 * 1024 * 1024))   # 4 MiB of zeros, a few KiB packed
        with self.assertRaises(ValueError):
            download.gunzip(bomb, cap=1024 * 1024)
        with self.assertRaises(ValueError):
            download.gunzip_stream(bomb, cap=1024 * 1024).read(-1)
        with mock.patch.object(download, "MAX_UNPACKED_BYTES", 1024 * 1024), \
                mock.patch.object(download.gunzip, "__defaults__", (1024 * 1024,)):
            with self.assertRaises(ValueError):
                list(apt.parse(bomb))

    def test_apt_is_https_and_names_follow_debian_policy(self):
        self.assertTrue(all(u.startswith("https://") for u in apt.urls("ubuntu:noble") + apt.urls("debian:bookworm")))
        with self.assertRaises(FetchError):
            fetch("http://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz")
        odd = APT_PACKAGES.replace("Package: nginx\n", "Package: Nginx;rm\n", 1)
        self.assertEqual(list(apt.parse(gzip.compress(odd.encode()))), [])
