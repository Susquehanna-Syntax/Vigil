"""GET /api/v1/metrics/recent/ — one request for many hosts' recent points.

The hosts page used to ask for five series per card and the Disk Pressure
widget one per host; this endpoint answers the whole fleet at once, so what
matters here is that it stays bounded (series, points, window), scoped to the
requester, and flat in query count as the fleet grows.
"""
from datetime import timedelta
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.timezone import now

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.metrics.models import MetricPoint
from vigil.testsupport import require_business_module

URL = "/api/v1/metrics/recent/"


def _make_host(name):
    return Host.objects.create(hostname=name, agent_token=f"tok-{name}")


def _point(host, category, metric, seconds_ago, value, labels=None):
    return MetricPoint.objects.create(
        host=host, time=now() - timedelta(seconds=seconds_ago),
        category=category, metric=metric, value=value, labels=labels or {})


class RecentMetricsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("ops-admin", password="pw")
        self.client.force_login(self.admin)
        self.series = "cpu/usage_percent:5,memory/usage_percent:5"

    def get(self, params=None, seconds_ago=300):
        """One request the way the browser makes it: `from` recent, so the
        points a test created with _point() at N seconds ago fall inside."""
        query = {"series": self.series, "from": now() - timedelta(seconds=seconds_ago)}
        if params is not None:
            query.update(params)
        return self.client.get(URL, query)

    def test_one_request_returns_every_series_for_every_host(self):
        hosts = [_make_host(f"r-{i}") for i in (1, 2)]
        # Oldest of the seven dropped (values 0 and 1), newest first in the body.
        for host in hosts:
            for i in range(7):
                _point(host, "cpu", "usage_percent", 30 * (6 - i), float(i))
            for i in range(2):
                _point(host, "memory", "usage_percent", 60 * (1 - i), float(i))

        body = self.get().json()
        self.assertEqual(sorted(body), sorted(str(h.id) for h in hosts))
        for key in body:
            cpu = body[key]["cpu/usage_percent"]
            self.assertEqual([p["value"] for p in cpu], [6.0, 5.0, 4.0, 3.0, 2.0])
            times = [p["time"] for p in cpu]
            self.assertEqual(times, sorted(times, reverse=True))
            mem = body[key]["memory/usage_percent"]
            self.assertEqual([p["value"] for p in mem], [1.0, 0.0])
            self.assertEqual(mem[0]["labels"], {})

    def test_the_recent_response_matches_the_api_schema(self):
        from vigil.schema_check import assert_shape

        from .api_schema import recent_host_entry

        host = _make_host("shape-1")
        _point(host, "cpu", "usage_percent", 60, 1.0, {"interface": "eth0"})
        resp = self.get()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        # The body's keys are host ids and `category/metric` pairs, so the
        # documented shape is a free-form mapping to one host's entry. With one
        # host and one series in the answer, that entry carries an item at every
        # level, so it — and through it each point — is really checked.
        assert_shape(self, body[str(host.id)], recent_host_entry())
        self.assertEqual(sorted(body), [str(host.id)])

    def test_old_points_are_left_out(self):
        host = _make_host("old-1")
        _point(host, "cpu", "usage_percent", 60, 9.0)
        _point(host, "cpu", "usage_percent", 600, 1.0)
        _point(host, "memory", "usage_percent", 600, 2.0)

        body = self.get(seconds_ago=300).json()
        cpu = body[str(host.id)]["cpu/usage_percent"]
        self.assertEqual([p["value"] for p in cpu], [9.0])
        self.assertNotIn("memory/usage_percent", body[str(host.id)])

    def test_host_filter(self):
        first, second = _make_host("f-1"), _make_host("f-2")
        for host in (first, second):
            _point(host, "cpu", "usage_percent", 60, 5.0)
            _point(host, "memory", "usage_percent", 60, 6.0)

        body = self.get({"host": str(first.id)}).json()
        self.assertEqual(list(body), [str(first.id)])

    def test_the_count_is_per_label_set(self):
        # A 16-core host writes the per-core points before the total, all at one
        # time: a plain newest-N would keep cores and lose the total the card shows.
        host = _make_host("cores-1")
        for ago in (60, 30):
            for core in [str(i) for i in range(16)] + ["total"]:
                _point(host, "cpu", "usage_percent", ago, float(ago), {"core": core})
            for iface in ("eth0", "eth1", "docker0"):
                _point(host, "network", "bytes_recv", ago, float(ago), {"interface": iface})
        self.series = "cpu/usage_percent:1,network/bytes_recv:2"
        body = self.get().json()[str(host.id)]
        cpu = body["cpu/usage_percent"]
        self.assertEqual(len(cpu), 17)
        self.assertEqual({p["value"] for p in cpu}, {30.0})
        self.assertEqual([p for p in cpu if p["labels"]["core"] == "total"][0]["value"], 30.0)
        net = body["network/bytes_recv"]
        for iface in ("eth0", "eth1", "docker0"):
            self.assertEqual([p["value"] for p in net if p["labels"]["interface"] == iface],
                             [30.0, 60.0])

    def test_named_hosts_are_never_cut_by_the_fleet_cap(self):
        from apps.metrics import views
        hosts = [_make_host(f"cap-{i}") for i in range(4)]
        for host in hosts:
            _point(host, "cpu", "usage_percent", 60, 1.0)
        with mock.patch.object(views, "MAX_RECENT_HOSTS", 2):
            # Named: the last two by name are what was asked for, and come back.
            body = self.get({"host": [str(h.id) for h in hosts[2:]]}).json()
            self.assertEqual(sorted(body), sorted(str(h.id) for h in hosts[2:]))
            self.assertEqual(self.get({"host": [str(h.id) for h in hosts[:3]]}).status_code, 400)
            # Unnamed: the first two by name, and a header saying there were more.
            resp = self.get()
            self.assertEqual(sorted(resp.json()), sorted(str(h.id) for h in hosts[:2]))
            self.assertEqual(resp["X-Vigil-Truncated"], "1")
        self.assertNotIn("X-Vigil-Truncated", self.get())

    def test_label_cardinality_cannot_outgrow_the_budgets(self):
        from apps.metrics import views
        host, other = _make_host("many-1"), _make_host("many-2")
        for i in range(12):
            _point(host, "disk", "usage_percent", 60, 1.0, {"mount": f"/m{i}"})
            _point(host, "disk", "usage_percent", 30, 2.0, {"mount": f"/m{i}"})
        _point(other, "disk", "usage_percent", 60, 1.0, {"mount": "/"})
        self.series = "disk/usage_percent:2"
        with mock.patch.object(views, "MAX_RECENT_POINTS_PER_HOST_SERIES", 10):
            pts = self.get({"host": str(host.id)}).json()[str(host.id)]["disk/usage_percent"]
            self.assertEqual(len(pts), 10)               # five mounts x two
        with mock.patch.object(views, "MAX_RECENT_POINTS_TOTAL", 7):
            resp = self.get()
            held = sum(len(s) for h in resp.json().values() for s in h.values())
            self.assertEqual(held, 7)
            self.assertEqual(resp["X-Vigil-Truncated"], "1")
        with mock.patch.object(views, "MAX_RECENT_POINTS_TOTAL", 0):
            # An exhausted budget allocates nothing: no empty hosts or series.
            self.assertEqual(self.get().json(), {})

    def test_a_host_that_is_not_an_id_is_400(self):
        resp = self.get({"host": "not-a-uuid"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("host", resp.json()["error"])

    def test_bad_input_is_400(self):
        recent = now() - timedelta(minutes=5)
        stale = now() - timedelta(hours=25)
        bad = [
            ("no series", {"from": recent}),
            ("empty series", {"series": "", "from": recent}),
            ("no point count", {"series": "cpu/usage_percent", "from": recent}),
            ("zero points", {"series": "cpu/usage_percent:0", "from": recent}),
            ("fifty-one points", {"series": "cpu/usage_percent:51", "from": recent}),
            ("nine series", {"series": ",".join(f"cpu/metric_{i}:1" for i in range(9)),
                             "from": recent}),
            ("uppercase category", {"series": "Cpu/x:1", "from": recent}),
            ("no from", {"series": self.series}),
            ("unparsable from", {"from": "notadate"}),
            ("from older than a day", {"from": stale}),
        ]
        for label, params in bad:
            with self.subTest(label):
                resp = self.client.get(URL, params)
                self.assertEqual(resp.status_code, 400, params)
                self.assertIn("error", resp.json())

    def test_query_count_does_not_grow_with_hosts(self):
        counts = []
        for total in (3, 30):
            MetricPoint.objects.all().delete()
            Host.objects.all().delete()
            for i in range(total):
                host = _make_host(f"q-{total}-{i}")
                _point(host, "cpu", "usage_percent", 60, 1.0)
                _point(host, "memory", "usage_percent", 60, 2.0)
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.get().status_code, 200)
            # The endpoint only reads. A request can also write session or
            # activity rows once enough time has passed, which a slow runner
            # gives it, and that has nothing to do with the number of hosts.
            counts.append(sum(1 for q in queries.captured_queries
                              if q["sql"].lstrip().upper().startswith("SELECT")
                              and "django_session" not in q["sql"]))
        self.assertEqual(counts[0], counts[1], f"3 hosts vs 30: {counts}")
        self.assertTrue(counts[0], "no read was captured")


# Exercises core with the Business apps; a source checkout has none.
require_business_module()


class RecentMetricsScopeTests(TestCase):
    """The endpoint is a person's request, not a system one: a site admin sees
    their own sites' hosts and learns nothing about the rest."""

    @classmethod
    def setUpClass(cls):
        # Resolved through the app registry rather than imported at module
        # scope: a source checkout has no Business apps, so its models must not
        # appear in the import block above. The app labels are the sites app's
        # registered ones, which is why "business_sites" names them.
        cls.Site = django_apps.get_model("business_sites", "Site")
        cls.HostSiteAssignment = django_apps.get_model("business_sites", "HostSiteAssignment")
        cls.UserSiteRole = django_apps.get_model("business_sites", "UserSiteRole")
        cls.Role = Role
        cls.UserProfile = UserProfile
        super().setUpClass()

    def setUp(self):
        User = get_user_model()
        self.west = self.Site.objects.create(name="West", slug="west")
        self.lab = self.Site.objects.create(name="Lab", slug="lab")
        self.dana = User.objects.create_user("dana", password="pw")
        self.UserProfile.objects.create(user=self.dana, role=self.Role.VIEWER)
        self.UserSiteRole.objects.create(user=self.dana, site=self.west,
                                         role=self.Role.ADMIN)
        self.client.force_login(self.dana)

        self.hosts = {}
        for name, site in (("west-1", self.west), ("lab-1", self.lab)):
            host = _make_host(name)
            self.HostSiteAssignment.objects.create(host=host, site=site)
            _point(host, "cpu", "usage_percent", 60, 3.0)
            self.hosts[name] = host
        self.query = {"series": "cpu/usage_percent:5", "from": now() - timedelta(minutes=5)}

    def test_scope(self):
        west, lab = self.hosts["west-1"], self.hosts["lab-1"]
        asked = self.client.get(URL, {**self.query, "host": str(lab.id)}).json()
        self.assertEqual(asked, {})

        body = self.client.get(URL, self.query).json()
        self.assertEqual(list(body), [str(west.id)])
