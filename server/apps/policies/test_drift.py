"""Policy drift: the decision table, host selection, and unknown hosts."""
import uuid

from apps_business.sites.models import HostSiteAssignment, Site
from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host
from apps.software.models import SoftwareItem, SoftwareSnapshot

from .drift import policy_drift, policy_hosts, rule_change
from .models import AppRule, UpdatePolicy
from .test_policy_api import admin_client


def make_host(name, tags=None, mode=Host.Mode.MANAGED, status=Host.Status.ONLINE,
              snapshot=True):
    host = Host.objects.create(hostname=name, mode=mode, status=status,
                               tags=tags or [], agent_token=uuid.uuid4().hex)
    if snapshot:
        SoftwareSnapshot.objects.create(host=host, item_count=0)
    return host


def give(host, package_id, version, latest="", source="dpkg"):
    return SoftwareItem.objects.create(
        host=host, source=source, package_id=package_id, name=package_id,
        name_key=package_id, version=version, latest_version=latest,
        first_seen=now())


class Row:
    def __init__(self, package_id, version, latest="", source="dpkg"):
        self.package_id, self.version, self.latest_version = package_id, version, latest
        self.source = source

    @property
    def outdated(self):
        return bool(self.latest_version) and self.latest_version != self.version


def rule(state, version="", source=""):
    return AppRule(app="curl", source=source, state=state, version=version)


class DecisionTableTests(TestCase):
    def test_every_cell(self):
        current = [Row("curl", "8.5", "8.5")]
        outdated = [Row("curl", "8.4", "8.5")]
        cases = [
            ("present", "", [], "install"),
            ("present", "", current, None),
            ("latest", "", [], "install"),
            ("latest", "", outdated, "upgrade"),
            ("latest", "", current, None),
            ("latest", "", [Row("curl", "8.4")], None),  # latest unknown
            ("pinned", "8.0", [], "install"),
            ("pinned", "8.0", current, "pin"),
            ("pinned", "8.5", current, None),
            ("absent", "", [], None),
            ("absent", "", current, "uninstall"),
        ]
        for state, version, items, expected in cases:
            with self.subTest(state=state, items=len(items), expected=expected):
                change = rule_change(rule(state, version), items)
                self.assertEqual(change and change["action"], expected)

    def test_wants_and_haves(self):
        up = rule_change(rule("latest"), [Row("curl", "8.4", "8.5")])
        self.assertEqual((up["have"], up["want"]), ("8.4", "8.5"))
        pin = rule_change(rule("pinned", "8.0"), [Row("curl", "8.5")])
        self.assertEqual((pin["have"], pin["want"]), ("8.5", "8.0"))
        new = rule_change(rule("pinned", "8.0"), [])
        self.assertEqual(new["want"], "8.0")

    def test_a_source_restricts_the_match(self):
        winget_only = rule("absent", source="winget")
        self.assertIsNone(rule_change(winget_only, [Row("curl", "8.5", source="dpkg")]))
        gone = rule_change(rule("absent"), [Row("curl", "8.5", source="registry")])
        self.assertEqual(gone["source"], "registry",
                         "the row's own source is what the agent must uninstall from")

    def test_another_app_never_matches(self):
        self.assertEqual(rule_change(rule("present"), [Row("curly", "1")])["action"],
                         "install")


class PolicyHostsTests(TestCase):
    def test_base_set_tags_and_mode(self):
        office = make_host("office-1", tags=["Office"])
        make_host("lab-1", tags=["lab"])
        make_host("watch-1", tags=["office"], mode=Host.Mode.MONITOR)
        make_host("new-1", tags=["office"], status=Host.Status.PENDING)
        make_host("bad-1", tags=["office"], status=Host.Status.REJECTED)
        policy = UpdatePolicy.objects.create(name="p", target_tags=["office"])
        self.assertEqual(list(policy_hosts(policy)), [office])
        everyone = UpdatePolicy.objects.create(name="all")
        self.assertEqual(sorted(h.hostname for h in policy_hosts(everyone)),
                         ["lab-1", "office-1"])

    def test_site_filter(self):
        glob = Site.objects.filter(is_global=True).first() or \
            Site.objects.create(name="Global", slug="global", is_global=True)
        west = Site.objects.create(name="West", slug="west")
        unassigned = make_host("free-1")
        in_west = make_host("west-1")
        HostSiteAssignment.objects.create(host=in_west, site=west)
        self.assertEqual(list(policy_hosts(
            UpdatePolicy.objects.create(name="w", site_id=west.pk))), [in_west])
        self.assertEqual(list(policy_hosts(
            UpdatePolicy.objects.create(name="g", site_id=glob.pk))), [unassigned])
        gone = UpdatePolicy.objects.create(name="x", site_id=uuid.uuid4())
        self.assertEqual(list(policy_hosts(gone)), [])


class PolicyDriftTests(TestCase):
    def test_drifted_compliant_and_unknown(self):
        policy = UpdatePolicy.objects.create(name="p")
        AppRule.objects.create(policy=policy, app="curl", state="latest")
        AppRule.objects.create(policy=policy, app="telnet", state="absent")
        stale = make_host("a-stale")
        give(stale, "curl", "8.4", "8.5")
        give(stale, "telnet", "0.17")
        fine = make_host("b-fine")
        give(fine, "curl", "8.5", "8.5")
        make_host("c-silent", snapshot=False)

        drift = policy_drift(policy)
        self.assertEqual(drift["compliant"], 1)
        self.assertEqual([u["hostname"] for u in drift["unknown"]], ["c-silent"])
        [row] = drift["hosts"]
        self.assertEqual(row["hostname"], "a-stale")
        self.assertEqual([c["action"] for c in row["changes"]], ["upgrade", "uninstall"])

    def test_endpoint_is_admin_only(self):
        policy = UpdatePolicy.objects.create(name="p")
        client, _ = admin_client()
        body = client.get(f"/api/v1/policies/{policy.pk}/drift/").json()
        self.assertEqual(body, {"hosts": [], "compliant": 0, "unknown": []})
        from apps.accounts.models import Role
        viewer, _ = admin_client("v", role=Role.VIEWER)
        self.assertEqual(viewer.get(f"/api/v1/policies/{policy.pk}/drift/").status_code, 403)
