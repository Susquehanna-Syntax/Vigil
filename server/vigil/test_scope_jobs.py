"""Every background entry point names its scope (decisions-06 item 17).

With no scope a site-scoped query fails closed: it raises under the test
runner and returns nothing in production. So each Celery task, management
command and thread target must say which scope it runs in, and these tests
fail for any that does not.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.test import RequestFactory, TestCase

from apps.accounts.models import Role, UserProfile
from vigil import scoping
from vigil.testsupport import require_business_module

require_business_module()

from apps_business.sites.models import Site, UserSiteRole  # noqa: E402


def _vigil_tasks():
    from celery import current_app

    current_app.loader.import_default_modules()
    for name, task in sorted(current_app.tasks.items()):
        module = getattr(task.run, "__module__", "") or ""
        if module.startswith(("apps.", "apps_business.", "vigil.")):
            yield name, task


class BackgroundJobsAreDeclaredTests(TestCase):
    def test_every_task_declares_its_scope(self):
        tasks = dict(_vigil_tasks())
        self.assertGreater(len(tasks), 20, sorted(tasks))
        undeclared = sorted(name for name, task in tasks.items()
                            if not getattr(task.run, "_vigil_scope_reason", None))
        self.assertEqual(undeclared, [], "Celery tasks with no @scoping.system_job (or as_user inside)")

    def test_a_declared_task_runs_from_no_scope(self):
        from apps.alerts.tasks import prune_old_alerts
        with scoping.no_scope_for_tests():
            prune_old_alerts()  # strict: an undeclared query would raise ScopeMissing

    def test_an_undeclared_job_fails_in_ci(self):
        from apps.hosts.models import Host

        def job():
            return list(Host.objects.all())

        with scoping.no_scope_for_tests(), self.assertRaises(scoping.ScopeMissing):
            job()

    def test_management_commands_run_in_a_system_scope(self):
        seen = {}

        class Probe(BaseCommand):
            def handle(self, *args, **options):
                scope = scoping.current_scope()
                seen["kind"] = scope.kind if scope else None

        with scoping.no_scope_for_tests():
            Probe().execute(no_color=True, force_color=False, skip_checks=True)
        self.assertEqual(seen["kind"], "system")


class ScopeBannerTests(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West", slug="west")
        User = get_user_model()
        self.ida = User.objects.create_user("ida", password="x")
        UserProfile.objects.create(user=self.ida, role=Role.ADMIN)
        self.dana = User.objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        cache.set(scoping.SCOPE_ALERT_KEY, {"model": "Host", "where": "apps/x.py:1 in job",
                                            "at": "2026-11-03T20:00:00"}, 60)
        self.addCleanup(cache.delete, scoping.SCOPE_ALERT_KEY)

    def _banners(self, user):
        from vigil.context_processors import scope_banners
        request = RequestFactory().get("/")
        request.user = user
        return scope_banners(request)["scope_banners"]

    def test_instance_admins_see_it_site_admins_do_not(self):
        shown = self._banners(self.ida)
        self.assertEqual(len(shown), 1)
        self.assertIn("apps/x.py:1 in job", shown[0]["message"])
        self.assertEqual(self._banners(self.dana), [])


class AsgiEntryPointsAreDeclaredTests(TestCase):
    """The websocket app runs outside Django's middleware, so it names its
    own scope; without one, remote sessions would read nothing in production."""

    def test_websocket_app_declares_its_scope(self):
        from apps.remote.ws import websocket_app
        self.assertTrue(getattr(websocket_app, "_vigil_scope_reason", None))

    def test_the_session_handlers_run_in_its_scope(self):
        import asyncio
        import uuid
        from unittest import mock

        from apps.remote import ws
        seen = {}

        async def handler(scope, receive, send, sid):
            current = scoping.current_scope()
            seen["kind"] = current.kind if current else None
            # And a real lookup the handlers make, from that scope.
            seen["session"] = await ws.sync_to_async(ws._load_session, thread_sensitive=True)(sid)

        sid = str(uuid.uuid4())
        path = f"/ws/session/{sid}/"
        self.assertTrue(ws.BROWSER_ROUTE.match(path))
        with scoping.no_scope_for_tests(), mock.patch.object(ws, "browser_socket", handler):
            asyncio.run(ws.websocket_app({"type": "websocket", "path": path}, None, None))
        self.assertEqual(seen, {"kind": "system", "session": None})
