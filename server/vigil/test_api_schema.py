"""The OpenAPI schema of /api/v1/ (sprint M5): a ratchet towards every endpoint
appearing with request and response types.

drf-spectacular reports each view it cannot type as a warning or error naming
the view's source file. TYPED lists the apps whose views produce none; an app
on it that regresses fails here. An app that comes out clean while still off
the list fails too, so the list never lags behind the code. The milestone's
finish line is PENDING being empty.

To type an app's endpoints: give each function view an ``@extend_schema``
(request and responses, a serializer or ``inline_serializer``), or a
``serializer_class`` on class views, until ``manage.py spectacular`` prints
nothing for that app; then move the app from PENDING to TYPED.
"""

import re
from collections import defaultdict

from django.test import SimpleTestCase, TestCase
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator

#: Apps whose every /api/v1/ view comes out typed.
TYPED: set[str] = {"agent_dist", "instance", "licensing"}

#: Apps still to type, each one phase of the work.
PENDING: set[str] = {
    "accounts", "agentprofiles", "aisuggest", "alerts",
    "automations", "catalog", "dashboards", "hosts", "jackil",
    "metrics", "playbooks", "policies", "relays", "remote",
    "reports", "reprovision", "software", "stacks", "statuspage", "tasks",
    "vulns",
    # Views in vigil/ itself (about, health), and the Business apps, which
    # live in the private Vigil_Business repository.
    "vigil", "apps_business",
}

#: Messages tied to no view file: a view mounted at two URLs gets the same
#: operation id twice. Only ever lower this.
UNATTRIBUTED_MAX = 1

_APP = re.compile(r"/server/(?:apps/([a-z_]+)/|(vigil)/)|/(apps_business)/")


def schema_problems() -> dict[str, list[str]]:
    """Generate the schema once, and return each app's warnings and errors."""
    GENERATOR_STATS.reset()
    with GENERATOR_STATS.silence():
        SchemaGenerator().get_schema(request=None, public=True)
    problems = defaultdict(list)
    for message in [*GENERATOR_STATS._warn_cache, *GENERATOR_STATS._error_cache]:
        match = _APP.search(message)
        problems[next(g for g in match.groups() if g) if match else ""].append(message)
    GENERATOR_STATS.reset()
    return problems


class ApiSchemaRatchetTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.problems = schema_problems()

    def test_typed_apps_stay_typed(self):
        regressed = {app: self.problems[app] for app in TYPED if self.problems.get(app)}
        self.assertEqual(regressed, {}, "an app on TYPED has untyped views again")

    def test_a_clean_app_moves_to_typed(self):
        clean = sorted(app for app in PENDING if not self.problems.get(app))
        self.assertEqual(clean, [], "these apps are typed now: move them to TYPED")

    def test_every_app_with_problems_is_tracked(self):
        untracked = sorted(app for app in self.problems
                           if app and app not in TYPED | PENDING)
        self.assertEqual(untracked, [], "add these apps to PENDING")

    def test_no_new_unattributed_problems(self):
        self.assertLessEqual(len(self.problems.get("", [])), UNATTRIBUTED_MAX,
                             self.problems.get(""))

    def test_the_lists_do_not_overlap(self):
        self.assertEqual(TYPED & PENDING, set())


class ApiSchemaAccessTests(TestCase):
    """The schema maps every endpoint: admins only."""

    def test_anonymous_and_non_admin_users_get_no_schema(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import Role, UserProfile

        for url in ("/api/schema/", "/api/docs/"):
            self.assertIn(self.client.get(url).status_code, (302, 401, 403), url)
        viewer = get_user_model().objects.create_user("viewer", password="pw")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        self.client.force_login(viewer)
        for url in ("/api/schema/", "/api/docs/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_an_admin_gets_the_schema_and_a_page_that_loads_nothing_external(self):
        from django.contrib.auth import get_user_model
        from apps.accounts.models import Role, UserProfile

        admin = get_user_model().objects.create_user("admin", password="pw")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_login(admin)
        schema = self.client.get("/api/schema/")
        self.assertEqual(schema.status_code, 200)
        self.assertIn(b"/api/v1/", schema.content)
        page = self.client.get("/api/docs/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b"https://", page.content)
        self.assertNotIn(b"<style", page.content)
