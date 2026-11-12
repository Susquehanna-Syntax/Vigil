"""Every place code crosses site scope, pinned (S1 phase 04a).

``Model.objects.unscoped(reason="...")`` and ``scoping.system(reason="...")``
are the only ways past a SiteScopedManager. This test finds each one in the
server tree and compares it with CROSSINGS, exactly: adding a crossing means
adding it here, where a reviewer sees it. The reason must be a string literal
so it can be read without running the code.
"""
import ast
from pathlib import Path

from django.test import SimpleTestCase

SERVER = Path(__file__).resolve().parent.parent

#: "relative/path.py" -> sorted reasons of the crossings in that file.
CROSSINGS: dict[str, list[str]] = {
    'apps/agentprofiles/redeem_views.py': ['agent install: a one-time profile token is the access control'],
    'apps/agentprofiles/views.py': ['agent profile names are unique across the install'],
    'apps/alerts/tasks.py': ['background job: acknowledgements expire on every site',
                             "background job: agent versions are checked for every site's hosts",
                             "background job: alert rules are evaluated for every site's hosts",
                             "background job: every site's silent hosts are marked offline",
                             "background job: image updates are checked for every site's hosts",
                             "background job: retention prunes every site's alerts",
                             "background job: retention prunes every site's metrics"],
    'apps/automations/engine.py': ['automation events fire install-wide, whoever raised them'],
    'apps/automations/tasks.py': ['background job: a scheduled automation fires, bounded by its own reach'],
    'apps/catalog/tasks.py': ['background job: the package catalog is install-wide'],
    'apps/instance/tasks.py': ['background job: a backup covers the whole install'],
    'vigil/metrics_endpoint.py': ['Prometheus scrape: no person is behind it; the bearer token is the access control'],
    'vigil/health_view.py': ['instance health: the counts are install-wide'],
    'apps/hosts/enrollment.py': ["re-enrolment moves the machine's own history onto its surviving row",
                                 're-enrolment: the same machine is matched across every site'],
    'apps/hosts/logtail.py': ['agent API: the caller is a host, authenticated by its agent token'],
    'apps/hosts/tasks.py': ['background job: directory import is install-wide'],
    'apps/hosts/views/_common.py': ['tag delete guard: a tag is install-wide',
                                    'tag rename: tags are install-wide',
                                    'tag rename: tags are install-wide',
                                    'tag rename: tags are install-wide'],
    'apps/hosts/views/agent.py': ['agent API: the caller is a host, authenticated by its agent token',
                                  'agent enrolment: a new host registers with no user'],
    'apps/licensing/tasks.py': ['background job: the licence is install-wide'],
    'apps/metrics/tasks.py': ['background job: database size is install-wide'],
    'apps/playbooks/expansion.py': ['playbook include: resolved by name on every site, then bounded by including_from'],
    'apps/playbooks/tasks.py': ['background job: auto-enrolment reconciles every playbook, each bounded by its own site'],
    'apps/playbooks/views.py': ['playbook names are unique across the install',
                                'playbook names are unique across the install',
                                'playbook names are unique across the install',
                                'playbook names are unique across the install'],
    'apps/policies/run.py': ['policy run: narrowing is by run permission, checked per host below'],
    'apps/policies/tasks.py': ['background job: a scheduled policy runs, bounded by its own site'],
    'apps/policies/validation.py': ['policy names are unique across the install'],
    'apps/remote/views.py': ['agent API: the caller is a host, authenticated by its agent token'],
    'apps/remote/ws.py': ["remote session socket: the session's own user, or its host's agent token, is the access control"],
    'apps/reprovision/installer_views.py': ['reprovision installer: the rebuild token is the access control',
                                            'reprovision installer: the rebuild token is the access control',
                                            'reprovision installer: the rebuild token is the access control'],
    'apps/reprovision/tasks.py': ['background job: downloading an OS image, which belongs to no site',
                                  'background job: importing an OS image, which belongs to no site',
                                  'background job: rebuild deadlines are swept on every site'],
    'apps/stacks/git_views.py': ['agent API: the caller is a host, authenticated by its agent token'],
    'apps/stacks/registries.py': ['agent API: the caller is a host, authenticated by its agent token'],
    'apps/stacks/views.py': ['agent API: the caller is a host, authenticated by its agent token',
                             'agent API: the caller is a host, authenticated by its agent token'],
    'apps/statuspage/tasks.py': ["background job: retention prunes every site's uptime samples",
                                 "background job: uptime is sampled for every site's hosts"],
    'apps/statuspage/views.py': ['free status page limit is per install',
                                 'public status page: the page token is the access control',
                                 'public status page: the page token is the access control'],
    'apps/tasks/authz.py': ['rollout writes: every host a rollout names must be runnable by the caller',
                            'run trimming: a run is trimmed when any of its tasks is on a site the caller cannot see'],
    'apps/tasks/tasks.py': ['background job: every rollout advances, each re-applying its own site',
                            'background job: stale tasks expire on every site'],
    'apps/tasks/views/agent.py': ['agent API: the caller is a host, authenticated by its agent token'],
    'apps/vulns/tasks.py': ["background job: daily vulnerability scores for every site's hosts",
                            "background job: retention prunes every site's score history",
                            "background job: scanner results are imported for every site's hosts",
                            'background job: the KEV list is install-wide',
                            "background job: vulnerability data is matched for every site's hosts"],
}


def _is_test(path):
    return path.name.startswith("test") or path.name == "testsupport.py"


def _system_names(tree):
    """Local names bound to vigil.scoping.system by a from-import."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "vigil.scoping":
            names |= {a.asname or a.name for a in node.names
                      if a.name in ("system", "system_view", "system_job")}
    return names


def _is_crossing(call, system_names):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "unscoped":
        return True
    if isinstance(f, ast.Attribute) and f.attr in ("system", "system_view", "system_job"):
        return isinstance(f.value, ast.Name) and f.value.id == "scoping"
    return isinstance(f, ast.Name) and f.id in system_names


def find_crossings(root=SERVER):
    found, bad = {}, []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if ".venv" in path.parts or "migrations" in path.parts or _is_test(path):
            continue
        if rel == "vigil/scoping.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "unscoped" not in text and "system" not in text:
            continue
        tree = ast.parse(text, filename=rel)
        system_names = _system_names(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_crossing(node, system_names)):
                continue
            reason = next((k.value for k in node.keywords if k.arg == "reason"), None)
            if not (isinstance(reason, ast.Constant) and isinstance(reason.value, str)):
                bad.append(f"{rel}:{node.lineno} has no literal reason")
                continue
            found.setdefault(rel, []).append(reason.value)
    return {k: sorted(v) for k, v in found.items()}, bad


class ScopeCrossingsTests(SimpleTestCase):
    def test_every_crossing_is_pinned(self):
        found, bad = find_crossings()
        self.assertEqual(bad, [], "a crossing must name its reason as a string literal")
        self.maxDiff = None
        self.assertEqual(found, CROSSINGS,
                         "site-scope crossings changed: review each one, then pin it in CROSSINGS")

    def test_the_scanner_sees_both_forms(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "apps").mkdir()
            (root / "apps" / "views.py").write_text(
                "from vigil import scoping\n"
                "a = Host.objects.unscoped(reason=\"agent ingest\")\n"
                "with scoping.system(reason='nightly purge'):\n    pass\n"
                "@scoping.system_view(reason='agent check-in')\ndef v(r):\n    pass\n"
                "b = Host.objects.unscoped(reason=why)\n"
                "import platform\nplatform.system()\n",
                encoding="utf-8")
            found, bad = find_crossings(root)
        self.assertEqual(found, {"apps/views.py": ["agent check-in", "agent ingest", "nightly purge"]})
        self.assertEqual(len(bad), 1)
        self.assertIn("apps/views.py:8", bad[0])


class AnonymousViewsAreDeclaredTests(SimpleTestCase):
    """Every DRF view open to anonymous callers (AllowAny) either runs in a
    system scope, with its reason on the callback, or is listed here as
    touching no site-owned rows. Once models are scoped, an anonymous request
    sees none of their rows, so an undeclared agent endpoint would quietly
    stop ingest; this test makes that a test failure instead."""

    #: Route -> why it reads no site-owned rows.
    NO_SITE_DATA = {
        "api/v1/health/": "liveness probe; reads nothing",
        "agent/download/<str:platform>/": "serves the agent binary; no host rows",
    }

    def test_every_allow_any_view_is_declared(self):
        from rest_framework.permissions import AllowAny

        from vigil.test_site_isolation import all_routes

        undeclared, stale = [], set(self.NO_SITE_DATA)
        for route, callback in all_routes():
            cls = getattr(callback, "cls", None)
            if cls is None or AllowAny not in getattr(cls, "permission_classes", ()):
                continue
            stale.discard(route)
            if getattr(callback, "_vigil_scope_reason", None):
                continue
            if route in self.NO_SITE_DATA:
                continue
            undeclared.append(route)
        self.assertEqual(sorted(undeclared), [],
                         "AllowAny views with no @scoping.system_view and no NO_SITE_DATA entry")
        self.assertEqual(sorted(stale), [], "stale NO_SITE_DATA entries")
