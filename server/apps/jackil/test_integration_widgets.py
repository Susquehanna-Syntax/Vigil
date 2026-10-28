"""What the widget endpoints hand a helpdesk: shapes, settings and scoping.

The envelope is the interesting part. Jackil draws these tiles from a fixed
contract and refuses anything else, so a test here mirrors that contract
rather than restating what the builder returned — a key we added by
accident fails here and not on someone's dashboard.
"""

import re
from datetime import timedelta
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskRun

from .integration_widgets import WIDGETS
from .test_integration import _admin, _authed

WIDGETS_URL = "/api/v1/jackil/integration/widgets/"
WIDGET = "/api/v1/jackil/integration/widgets/{}/"

#: The contract's mirror: what Jackil will accept per shape. Anything outside
#: these key sets is a payload the receiving side throws away.
_KEYS = {
    "stat": {"value", "label", "tone", "href", "sub"},
    "list": {"items", "empty"},
    "gauge": {"value", "label", "tone", "sub"},
}
_ITEM_KEYS = {"title", "href", "meta", "badge", "tone"}
_TONES = {"ok", "warn", "bad", "info", "muted"}


def _assert_contract(envelope):
    """Raise unless the envelope is one Jackil will draw."""
    assert set(envelope) == {"version", "shape", "data"}, sorted(envelope)
    assert envelope["version"] == 1
    shape = envelope["shape"]
    assert shape in _KEYS, shape
    data = envelope["data"]
    assert isinstance(data, dict), data
    extra = set(data) - _KEYS[shape]
    assert not extra, f"{shape} carries {sorted(extra)}"
    for key, value in data.items():
        assert key in _KEYS[shape], f"{shape} carries {key!r}"
        if key == "tone":
            assert value in _TONES, value
        elif key == "items":
            assert isinstance(value, list), value
            for item in value:
                missing = set(item) - _ITEM_KEYS
                assert not missing, f"item carries {sorted(missing)}"
                assert isinstance(item["title"], str), item
                # An optional part of a row is present with real content or
                # not present at all — never an empty string or a bare
                # null standing in for "nothing to show".
                for optional in ("href", "meta", "badge"):
                    assert optional not in item or item[optional], item
                assert "tone" not in item or item["tone"] in _TONES, item
        elif key == "value" and shape == "gauge":
            assert value is None or (isinstance(value, (int, float))
                                     and 0 <= value <= 100), value
        elif key == "value":
            assert isinstance(value, int) and not isinstance(value, bool), value
        elif key in ("empty", "label"):
            assert isinstance(value, str) and value, value
    if shape == "list":
        assert "empty" in data, "a list must say what to show when it has nothing"


class CatalogueTests(TestCase):
    def setUp(self):
        self.client = _authed(user=_admin("boss"))

    def test_catalogue_lists_every_kind_with_its_settings(self):
        body = self.client.get(WIDGETS_URL).json()
        self.assertEqual(body["version"], 1)
        self.assertEqual([row["kind"] for row in body["widgets"]], list(WIDGETS))
        for row in body["widgets"]:
            self.assertEqual(sorted(row), ["description", "kind", "settings", "shape", "title"])
            self.assertEqual(row["shape"], WIDGETS[row["kind"]]["shape"])
            for name, meta in row["settings"].items():
                self.assertEqual(meta["type"], WIDGETS[row["kind"]]["settings"][name]["type"])
                if meta["type"] == "choice":
                    self.assertIn("choices", meta)
                    self.assertIn("default", meta)
                else:
                    self.assertEqual((meta["min"], meta["max"], meta["default"]), (1, 20, 8))
        stat = next(row for row in body["widgets"] if row["kind"] == "stat")
        self.assertEqual(
            [choice[0] for choice in stat["settings"]["metric"]["choices"]],
            ["hosts_total", "hosts_offline", "alerts_firing", "alerts_critical",
             "runs_failed_24h", "pending_enrollments"])
        self.assertEqual(stat["settings"]["metric"]["default"], "alerts_firing")
        self.assertEqual(stat["settings"]["metric"]["choices"][2],
                         ["alerts_firing", "Firing alerts"])

    def test_each_widget_returns_a_contract_envelope(self):
        for kind in WIDGETS:
            with self.subTest(kind=kind):
                response = self.client.get(WIDGET.format(kind))
                self.assertEqual(response.status_code, 200, response.content)
                _assert_contract(response.json())

    def test_settings_come_back_as_the_settings_they_take(self):
        """A widget with no settings ignores the ones it was not given, and
        one with them takes them as query parameters."""
        self.assertEqual(self.client.get(WIDGET.format("hosts_online"),
                                         {"limit": "3"}).status_code, 200)


class SettingTests(TestCase):
    def setUp(self):
        self.client = _authed(user=_admin("boss"))

    def test_unknown_widget_404_unknown_metric_400_bad_limit_400_and_limit_clamped(self):
        missing = self.client.get(WIDGET.format("containers"))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"error": "Unknown widget"})

        bad = self.client.get(WIDGET.format("stat"), {"metric": "cpu_load"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json(), {"error": "Unknown metric"})

        for limit in ("many", "1.5", ""):
            expected = 400 if limit else 200
            with self.subTest(limit=limit):
                response = self.client.get(WIDGET.format("alert_list"), {"limit": limit})
                self.assertEqual(response.status_code, expected, response.content)
                if expected == 400:
                    self.assertEqual(response.json(),
                                     {"error": "limit must be a whole number"})

        # Out of range is a wish we cannot fully grant, not a bad setting.
        for n in range(25):
            Host.objects.create(hostname=f"h{n:02d}", agent_token=f"t-{n:02d}",
                                status=Host.Status.OFFLINE)
        for limit, want in (("0", 1), ("-4", 1), ("500", 20)):
            with self.subTest(limit=limit):
                response = self.client.get(WIDGET.format("offline_hosts"), {"limit": limit})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["data"]["items"]), want)

    def test_an_empty_metric_falls_back_to_the_default(self):
        """`?metric=` with nothing after it is the browser's own empty
        field, and means "whatever you'd have picked"."""
        body = self.client.get(WIDGET.format("stat"), {"metric": ""}).json()
        self.assertEqual(body["data"]["label"], "Firing alerts")


class GaugeTests(TestCase):
    def setUp(self):
        self.client = _authed(user=_admin("boss"))

    def _gauge(self):
        return self.client.get(WIDGET.format("hosts_online")).json()["data"]

    def test_hosts_online_is_none_without_hosts_and_a_percentage_with_them(self):
        data = self._gauge()
        self.assertIsNone(data["value"])
        self.assertEqual(data["label"], "Hosts online")
        self.assertEqual(data["tone"], "muted")
        self.assertNotIn("sub", data, "a percentage with no denominator has no '12 of 14'")
        _assert_contract({"version": 1, "shape": "gauge", "data": data})

        # Pending and rejected hosts are enrolled-but-not-reporting states,
        # neither of which belongs in a gauge about reporting hosts.
        Host.objects.create(hostname="p", agent_token="t-p", status=Host.Status.PENDING)
        Host.objects.create(hostname="r", agent_token="t-r", status=Host.Status.REJECTED)
        self.assertIsNone(self._gauge()["value"])

        for n in range(12):
            Host.objects.create(hostname=f"on{n}", agent_token=f"t-on{n}",
                                status=Host.Status.ONLINE)
        Host.objects.create(hostname="off", agent_token="t-off", status=Host.Status.OFFLINE)
        data = self._gauge()
        self.assertEqual(data["value"], 92.3)
        self.assertEqual(data["sub"], "12 of 13 online")
        self.assertEqual(data["tone"], "warn")

    def test_tone_boundaries(self):
        for online, offline, want in ((20, 0, "ok"), (19, 1, "ok"), (18, 2, "warn"),
                                      (16, 4, "warn"), (15, 5, "bad"), (10, 10, "bad")):
            with self.subTest(online=online, offline=offline):
                Host.objects.all().delete()
                for n in range(online):
                    Host.objects.create(hostname=f"on{n}", agent_token=f"t-on{n}",
                                        status=Host.Status.ONLINE)
                for n in range(offline):
                    Host.objects.create(hostname=f"off{n}", agent_token=f"t-off{n}",
                                        status=Host.Status.OFFLINE)
                self.assertEqual(self._gauge()["tone"], want)


class StatTests(TestCase):
    def setUp(self):
        self.client = _authed(user=_admin("boss"))
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32,
                                        status=Host.Status.OFFLINE)
        Host.objects.create(hostname="p", agent_token="t-p", status=Host.Status.PENDING)
        rule = AlertRule.objects.create(name="cpu", category="cpu", metric="cpu",
                                        operator="gt", threshold=90, severity="critical")
        Alert.objects.create(host=self.host, rule=rule, severity="critical",
                             message="disk full")
        run = TaskRun.objects.create(name_snapshot="r", host_count=1, state="failed")
        Task.objects.create(host=self.host, run=run, action="check_service",
                            nonce="n", params={})

    def _stat(self, metric):
        return self.client.get(WIDGET.format("stat"), {"metric": metric}).json()["data"]

    def test_stat_metrics_and_tones(self):
        # One of each thing exists, and the pending host counts twice: once as
        # a host, once as an enrollment waiting for someone.
        expected = {
            "hosts_total": (2, "Hosts", "info"),
            "hosts_offline": (1, "Offline hosts", "bad"),
            "alerts_firing": (1, "Firing alerts", "warn"),
            "alerts_critical": (1, "Critical alerts", "bad"),
            "runs_failed_24h": (1, "Failed runs (24h)", "bad"),
            "pending_enrollments": (1, "Pending enrollments", "warn"),
        }
        for metric, (value, label, tone) in expected.items():
            with self.subTest(metric=metric):
                data = self._stat(metric)
                self.assertEqual((data["value"], data["label"], data["tone"]),
                                 (value, label, tone))
                _assert_contract({"version": 1, "shape": "stat", "data": data})

    def test_zero_tones_are_ok_except_hosts_total(self):
        # One online host stays behind: a fleet of nothing at all is the
        # gauge's story to tell, not this metric's.
        Host.objects.create(hostname="up", agent_token="t-up", status=Host.Status.ONLINE)
        Alert.objects.all().delete()
        TaskRun.objects.all().delete()
        Host.objects.filter(status=Host.Status.OFFLINE).delete()
        Host.objects.filter(status=Host.Status.PENDING).delete()
        for metric, want in (("hosts_offline", "ok"), ("alerts_firing", "ok"),
                             ("alerts_critical", "ok"), ("runs_failed_24h", "ok"),
                             ("pending_enrollments", "ok"), ("hosts_total", "info")):
            with self.subTest(metric=metric):
                data = self._stat(metric)
                self.assertEqual(data["value"], 1 if metric == "hosts_total" else 0)
                self.assertEqual(data["tone"], want)

    def test_a_failure_older_than_the_window_is_not_a_failure_today(self):
        run = TaskRun.objects.get()
        TaskRun.objects.filter(pk=run.pk).update(
            created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self._stat("runs_failed_24h")["value"], 0)

    def test_an_acknowledged_alert_still_counts_as_open_but_not_as_firing(self):
        alert = Alert.objects.get()
        Alert.objects.filter(pk=alert.pk).update(state=Alert.State.ACKNOWLEDGED)
        self.assertEqual(self._stat("alerts_firing")["value"], 0)
        self.assertEqual(self._stat("alerts_critical")["value"], 1)


class ListTests(TestCase):
    def setUp(self):
        self.owner = _admin("owner")
        self.client = _authed(user=self.owner)
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32,
                                        os="Ubuntu", status=Host.Status.OFFLINE,
                                        last_checkin=timezone.now() - timedelta(days=3))
        self.never = Host.objects.create(hostname="old1", agent_token="t-old",
                                         status=Host.Status.OFFLINE)
        rule = AlertRule.objects.create(name="cpu", category="cpu", metric="cpu",
                                        operator="gt", threshold=90, severity="warning")
        self.alert = Alert.objects.create(host=self.host, rule=rule, severity="warning",
                                          message="x" * 120)
        self.run = TaskRun.objects.create(name_snapshot="Nightly run", host_count=2,
                                          state="failed")
        Task.objects.create(host=self.host, run=self.run, action="check_service",
                            nonce="n", params={})

    def test_alert_and_run_lists_link_to_deep_links(self):
        alerts = self.client.get(WIDGET.format("alert_list")).json()["data"]["items"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["href"], f"http://testserver/#open/host/{self.host.id}")
        self.assertEqual(alerts[0]["title"], f"web1 · {'x' * 80}")
        self.assertEqual(alerts[0]["badge"], "Firing")
        self.assertEqual(alerts[0]["tone"], "warn")
        self.assertTrue(alerts[0]["meta"].startswith("Warning · "), alerts[0]["meta"])

        runs = self.client.get(WIDGET.format("recent_runs")).json()["data"]["items"]
        self.assertEqual(runs[0]["href"], f"http://testserver/#open/run/{self.run.id}")
        self.assertEqual(runs[0]["title"], "Nightly run")
        self.assertEqual(runs[0]["badge"], "Failed")
        self.assertEqual(runs[0]["tone"], "bad")
        self.assertEqual(runs[0]["meta"], f"{self.run.created_at:%Y-%m-%d} · 2 hosts")

    def test_lists_say_their_own_thing_when_there_is_nothing(self):
        Alert.objects.all().delete()
        TaskRun.objects.all().delete()
        Host.objects.filter(status=Host.Status.OFFLINE).delete()
        for kind, empty in (("alert_list", "No open alerts."),
                            ("recent_runs", "No task runs yet."),
                            ("offline_hosts", "Every host is online.")):
            with self.subTest(kind=kind):
                data = self.client.get(WIDGET.format(kind)).json()["data"]
                self.assertEqual(data["items"], [])
                self.assertEqual(data["empty"], empty)

    def test_offline_hosts_oldest_silence_first_and_never_seen_leads(self):
        items = self.client.get(WIDGET.format("offline_hosts")).json()["data"]["items"]
        self.assertEqual([row["title"] for row in items], ["old1", "web1"])
        self.assertEqual(items[0]["meta"], "Never seen")
        self.assertEqual(items[1]["meta"],
                         f"Last seen {(self.host.last_checkin).date().isoformat()}")
        self.assertEqual(items[0]["tone"], "bad")

    def test_resolved_alerts_are_not_open(self):
        Alert.objects.update(state=Alert.State.RESOLVED)
        self.assertEqual(self.client.get(WIDGET.format("alert_list")).json()["data"]["items"], [])

    def test_a_newer_alert_is_listed_first(self):
        newer = Alert.objects.create(host=self.host, rule=self.alert.rule,
                                     severity="info", message="newer")
        Alert.objects.filter(pk=self.alert.pk).update(
            fired_at=timezone.now() - timedelta(days=1))
        items = self.client.get(WIDGET.format("alert_list")).json()["data"]["items"]
        self.assertEqual([row["title"] for row in items],
                         ["web1 · newer", f"web1 · {'x' * 80}"])
        self.assertEqual(items[0]["tone"], "info")
        self.assertIsNotNone(newer.pk)

    def test_limit_trims_the_list_to_the_rows_asked_for(self):
        for n in range(10):
            Host.objects.create(hostname=f"off{n}", agent_token=f"t-off{n}",
                                status=Host.Status.OFFLINE)
        items = self.client.get(WIDGET.format("offline_hosts"),
                                {"limit": "3"}).json()["data"]["items"]
        self.assertEqual(len(items), 3)

    def test_the_default_list_is_eight_rows(self):
        for n in range(12):
            Host.objects.create(hostname=f"off{n}", agent_token=f"t-off{n}",
                                status=Host.Status.OFFLINE)
        items = self.client.get(WIDGET.format("offline_hosts")).json()["data"]["items"]
        self.assertEqual(len(items), 8)


class AuthTests(TestCase):
    def test_widgets_need_the_key(self):
        keyless = _authed()
        keyless.credentials()
        self.assertEqual(keyless.get(WIDGETS_URL).status_code, 401)
        self.assertEqual(keyless.get(WIDGET.format("stat")).status_code, 401)

        session = _authed(user=_admin("boss"))
        session.credentials()
        admin = _admin("boss2")
        session.force_login(admin)
        for url in (WIDGETS_URL, WIDGET.format("stat")):
            with self.subTest(url=url):
                self.assertIn(session.get(url).status_code, (401, 403))

        with_key = _authed(user=admin)
        self.assertEqual(with_key.post(WIDGETS_URL, {}, format="json").status_code, 405)


REPO = Path(__file__).resolve().parents[3]
JS = REPO / "server" / "static" / "js"
TEMPLATES = REPO / "server" / "templates"


def _read(path: Path) -> str:
    return path.read_text()


class WiringTests(SimpleTestCase):
    """The three halves of this phase — the HTML host, the code that fills it,
    and the pane open that calls it — live in three files nothing compiles
    together. A rename anywhere in that chain is silent until someone opens
    Settings and finds a card that says "Loading…" forever."""

    def test_keys_card_and_deep_link_script_are_wired(self):
        settings_html = _read(TEMPLATES / "pages" / "_settings.html")
        self.assertIn('id="jackil-keys"', settings_html)

        instance_js = _read(JS / "vigil-instance-settings.js")
        self.assertIn("async function loadJackilKeys(", instance_js)
        for attribute in ("data-jk-create", "data-jk-revoke"):
            # Rendered markup and a delegateClick selector: the classic silent
            # failure is one of the two being renamed.
            self.assertIn(attribute, instance_js)
            pattern = re.compile(r"delegateClick\(\s*['\"`][^'\"`]*" + attribute)
            self.assertIsNotNone(pattern.search(instance_js),
                                 f"{attribute} appears in markup but no delegateClick binds it")

        settings_js = _read(JS / "vigil-settings.js")
        self.assertIn("loadJackilKeys()", settings_js)

        base = _read(TEMPLATES / "base.html")
        self.assertLess(base.index("vigil-search.js"), base.index("vigil-deeplink.js"),
                        "the deep-link opener must load after the page modules it calls")

        deeplink = _read(JS / "vigil-deeplink.js")
        for kind in ("host", "task", "playbook", "run", "automation"):
            self.assertIn(kind, deeplink)
        self.assertIn("replaceState", deeplink)

        automations = _read(JS / "vigil-automations.js")
        body = automations[automations.index("async function loadAutomations()"):]
        body = body[:body.index("\n}")]
        self.assertIn("return", body,
                      "the deep-link opener needs loadAutomations to hand back its rows")
