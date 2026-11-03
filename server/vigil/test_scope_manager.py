"""S1 phase 04a: the scope context and SiteScopedManager.

No model uses the manager yet (04b switches them), so these tests attach one
to Host and Alert by hand.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, TestCase

from apps.accounts.models import Role
from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import HostSiteAssignment, Site, UserSiteRole  # noqa: E402


def _manager(model, **kw):
    m = scoping.SiteScopedManager(**kw)
    m.model = model
    return m


class _TwoSites(TestCase):
    """West (dana administers it) and Lab, one host and one alert each."""

    @classmethod
    def setUpTestData(cls):
        west = Site.objects.create(name="West", slug="west")
        lab = Site.objects.create(name="Lab", slug="lab")
        cls.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=cls.dana, site=west, role=Role.ADMIN)
        cls.owner = get_user_model().objects.create_superuser("root", "r@example.com", "x")
        rule = AlertRule.objects.create(name="cpu", category="cpu", metric="cpu",
                                        operator="gt", threshold=90, severity="warning")
        for name, site in (("west-1", west), ("lab-1", lab)):
            h = Host.objects.create(hostname=name, agent_token=f"tok-{name}")
            HostSiteAssignment.objects.create(host=h, site=site)
            Alert.objects.create(host=h, rule=rule, severity="warning", message=name)
        cls.hosts = _manager(Host)
        cls.alerts = _manager(Alert, path="host__")

    def names(self, qs):
        return sorted(qs.values_list("hostname", flat=True))


class _FromNoScope:
    """Start each test from no scope at all; the runner otherwise gives test
    bodies a system scope (vigil/testrunner.py)."""

    def setUp(self):
        super().setUp()
        ctx = scoping.no_scope_for_tests()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)


class ScopedManagerTests(_FromNoScope, _TwoSites):

    def test_no_scope_fails_closed(self):
        # decisions-06 item 17: an undeclared path gets nothing, never every
        # site's rows. Strict (CI) raises and names the caller.
        self.assertIsNone(scoping.current_scope())
        with self.assertRaises(scoping.ScopeMissing) as caught:
            list(self.hosts.all())
        self.assertIn("test_scope_manager.py", str(caught.exception))

    def test_no_scope_in_production_is_empty_logged_and_flagged(self):
        from django.core.cache import cache
        from django.test import override_settings
        cache.delete(scoping.SCOPE_ALERT_KEY)
        with override_settings(VIGIL_SCOPE_STRICT=False), self.assertLogs("vigil.scoping", "ERROR"):
            self.assertEqual(list(self.hosts.all()), [])
        alert = scoping.scope_alert()
        self.assertEqual(alert["model"], "Host")
        self.assertIn("test_scope_manager.py", alert["where"])

    def test_user_scope_filters(self):
        with scoping.as_user(self.dana):
            self.assertEqual(self.names(self.hosts.all()), ["west-1"])
            self.assertEqual(list(self.alerts.values_list("message", flat=True)), ["west-1"])
            self.assertFalse(self.hosts.filter(hostname="lab-1").exists())
        with scoping.as_user(self.owner):
            self.assertEqual(self.names(self.hosts.all()), ["lab-1", "west-1"])

    def test_anonymous_and_missing_user_see_nothing(self):
        with scoping.as_user(AnonymousUser()):
            self.assertEqual(list(self.hosts.all()), [])
        with scoping.as_user(None):
            self.assertEqual(list(self.hosts.all()), [])

    def test_system_scope_needs_a_reason(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                scoping.system(reason=bad)
        with scoping.as_user(self.dana):
            with scoping.system(reason="agent check-in"):
                self.assertEqual(self.names(self.hosts.all()), ["lab-1", "west-1"])
            # Leaving the block restores the user scope.
            self.assertEqual(self.names(self.hosts.all()), ["west-1"])
        self.assertIsNone(scoping.current_scope())

    def test_unscoped_needs_a_reason(self):
        with scoping.as_user(self.dana):
            with self.assertRaises(ValueError):
                self.hosts.unscoped(reason="")
            self.assertEqual(self.names(self.hosts.unscoped(reason="test")), ["lab-1", "west-1"])

    def test_site_roles_are_looked_up_once_per_scope(self):
        with scoping.as_user(self.dana):
            list(self.hosts.all())
            with self.assertNumQueries(1):  # the host query only, no roles query
                list(self.hosts.all())
        # A new scope looks them up afresh (roles, the global site, then the
        # hosts), so a changed grant is seen.
        with scoping.as_user(self.dana):
            with self.assertNumQueries(3):
                list(self.hosts.all())

    def test_related_managers_keep_the_declared_path(self):
        # Django builds a reverse manager (rule -> alerts) by calling the
        # model's manager class with no arguments; the declared path must
        # still apply, not a default "" that filters Alert as if it were Host.
        rule = AlertRule.objects.get(name="cpu")
        accessor = Alert._meta.get_field("rule").remote_field.get_accessor_name()
        with scoping.as_user(self.dana):
            messages = list(getattr(rule, accessor).values_list("message", flat=True))
            self.assertEqual(messages, ["west-1"])
            # unscoped() on a related manager lifts only the site filter.
            every = getattr(rule, accessor).unscoped(reason="test")
            self.assertEqual(sorted(every.values_list("message", flat=True)), ["lab-1", "west-1"])
            self.assertIsNotNone(scoping.current_scope())
            self.assertEqual(scoping.current_scope().kind, "user")

    def test_scope_is_reset_after_an_exception(self):
        with self.assertRaises(RuntimeError):
            with scoping.as_user(self.dana):
                raise RuntimeError
        self.assertIsNone(scoping.current_scope())


class SiteScopeMiddlewareTests(_FromNoScope, _TwoSites):
    """The middleware scopes by whoever request.user is when the query runs."""

    def run_request(self, user, view):
        request = RequestFactory().get("/")
        request.user = user
        response = scoping.SiteScopeMiddleware(view)(request)
        self.assertIsNone(scoping.current_scope())
        return response

    def listing(self, request):
        return HttpResponse(",".join(self.names(self.hosts.all())))

    def test_request_is_scoped_to_its_user(self):
        self.assertEqual(self.run_request(self.dana, self.listing).content, b"west-1")
        self.assertEqual(self.run_request(AnonymousUser(), self.listing).content, b"")
        self.assertEqual(self.run_request(self.owner, self.listing).content, b"lab-1,west-1")

    def test_user_set_during_the_view_is_used(self):
        # DRF authenticates API keys inside the view and writes the user back
        # onto the Django request; the scope must follow it.
        def view(request):
            request.user = self.dana
            return self.listing(request)
        self.assertEqual(self.run_request(AnonymousUser(), view).content, b"west-1")

    def test_streaming_body_stays_scoped(self):
        def rows():
            # The query runs only when Django iterates the body, after the
            # middleware has returned.
            for h in self.hosts.all():
                yield f"{h.hostname}\n"

        def view(request):
            return StreamingHttpResponse(rows())
        response = self.run_request(self.dana, view)
        self.assertIsNone(scoping.current_scope())
        self.assertEqual(b"".join(response.streaming_content), b"west-1\n")

    def test_installed(self):
        from django.conf import settings
        mw = settings.MIDDLEWARE
        self.assertIn("vigil.scoping.SiteScopeMiddleware", mw)
        self.assertGreater(mw.index("vigil.scoping.SiteScopeMiddleware"),
                           mw.index("django.contrib.auth.middleware.AuthenticationMiddleware"))


class ScopedValuesKeepDuplicatesTests(_TwoSites):
    """The site filter joins one-to-one assignments, so it never repeats a row and
    must not add DISTINCT: inside a request that would collapse equal values in
    values_list() (two warnings counted as one) and make delete() refuse."""

    def test_values_list_keeps_equal_values(self):
        west = Host.objects.get(hostname="west-1")
        Alert.objects.create(host=west, rule=Alert.objects.first().rule, severity="warning", message="again")
        with scoping.as_user(self.dana):
            # order_by() drops the default ordering, as grouping code does and as
            # models without Meta.ordering (MetricPoint) always are.
            self.assertEqual(list(self.alerts.order_by().values_list("severity", flat=True)),
                             ["warning", "warning"])
            self.assertEqual(self.alerts.filter(severity="warning").count(), 2)

    def test_scoped_delete_works_and_stays_in_scope(self):
        with scoping.as_user(self.dana):
            self.alerts.all().delete()
        self.assertEqual(list(Alert.objects.values_list("message", flat=True)), ["lab-1"])


class ScopedPathsAreSingleValuedTests(TestCase):
    """filter_by_site adds no DISTINCT, which is only correct while every scoped
    path is single-valued: a path through a reverse FK or many-to-many would
    repeat rows. This pins that for every model that declares the manager."""

    def test_every_declared_path_is_single_valued(self):
        from django.apps import apps

        checked = 0
        for model in apps.get_models():
            mgr = model._meta.managers_map.get("objects")
            if not isinstance(mgr, scoping.SiteScopedManager) or mgr.site_field:
                continue
            current = model
            for name in [p for p in mgr.site_path.split("__") if p] + ["site_assignment"]:
                field = current._meta.get_field(name)
                self.assertTrue(
                    field.many_to_one or field.one_to_one,
                    f"{model.__name__}: '{name}' in path '{mgr.site_path}' is not single-valued")
                current = field.related_model
            checked += 1
        self.assertGreater(checked, 30)


class ReEnrolmentKeepsHistoryTests(_TwoSites):
    """Approving a re-enrolment moves the pending row's history onto the
    surviving host. The approver may not see the pending row's site (it is
    unassigned until approved), and that must not leave the history behind."""

    def test_west_admin_approval_moves_metrics_and_inventory(self):
        from django.utils import timezone

        from apps.hosts.enrollment import adopt_enrolment
        from apps.hosts.models import HostInventory
        from apps.metrics.models import MetricPoint

        existing = Host.objects.get(hostname="west-1")
        pending = Host.objects.create(hostname="west-1", agent_token="tok-new", status="pending")
        MetricPoint.objects.create(host=pending, time=timezone.now(), category="cpu",
                                   metric="usage_percent", value=5)
        HostInventory.objects.create(host=pending)
        with scoping.as_user(self.dana):
            adopt_enrolment(pending, existing)
        self.assertEqual(MetricPoint.objects.filter(host=existing).count(), 1)
        self.assertTrue(HostInventory.objects.filter(host=existing).exists())
