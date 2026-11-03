"""Walks every URL pattern Django knows and asks it, as a user confined to one
site, whether anything from another site comes back.

docs/site-scoping.md: a missed filter on any one of ~280 endpoints is a cross-site
leak. Checking them by hand is how five got covered and the rest did not, so this
module checks all of them by construction — it resolves every pattern, fills its
kwargs from site B's fixtures, and requires that a site-B object be
indistinguishable from an object that does not exist.

Nothing here fixes a leak. Each one it finds is recorded in `KNOWN_LEAKS`, which
the test pins exactly: the table shrinks as later phases fix them, a new leak
fails the run, and an empty table is the milestone's finish line.
"""

import re
import base64
import json
import time
import uuid
from datetime import timedelta
from unittest import mock

from nacl.signing import SigningKey

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import get_resolver
from django.urls import path as django_path
from django.urls.resolvers import URLResolver
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.accounts.models import Role
from apps.agentprofiles.models import AgentProfile
from apps.alerts.models import Alert, AlertRule
from apps.automations.models import Automation
from apps.dashboards.models import Dashboard
from apps.hosts.models import Host, LogTailSession, Tag, UnmanagedDevice
from apps.jackil.models import IntegrationKey
from apps.metrics.models import MetricPoint
from apps.playbooks.models import Playbook
from apps.policies.models import PolicyChange, UpdatePolicy
from apps.relays.models import Relay
from apps.remote.models import RemoteSession
from apps.reports.models import Report
from apps.reprovision.models import InstallProfile, OSImage, RebuildJob
from apps.software.models import SoftwareItem
from apps.stacks.models import (
    GitCredential,
    ManagedStack,
    RegistryCredential,
    StackRevision,
)
from apps.statuspage.models import HostUptimeSample, StatusPage
from apps.tasks.models import PatchWave, Task, TaskDefinition, TaskRun
from apps.vulns.models import VulnFinding, VulnScoreHistory
from vigil import licensing
from vigil.testsupport import require_business_module

# Exercises core with the Business apps; a source checkout has none.
require_business_module()
from apps_business.sites.models import (
    AutomationSiteAssignment,
    HostSiteAssignment,
    PlaybookSiteAssignment,
    Site,
    UserSiteRole,
)
from apps_business.webhooks.models import Webhook

#: The site the fixture logs in as, and the one that must stay invisible.
MINE, THEIRS = "west", "zzlab"

#: Every fixture row names itself after its site, so a leak of any site-B row
#: into any response body is visible as text, without per-endpoint parsing.
LEAK_MARKER = THEIRS

#: kwarg name -> how to fill it. A callable gets the site's object dict; a bare
#: string or int is the *same* value in resp_b and resp_x, which is what makes
#: the two responses comparable at all.
FILLERS = {
    "host_id": lambda o: o["host"].id,
    "alert_id": lambda o: o["alert"].id,
    "task_id": lambda o: o["task"].id,
    "stack_id": lambda o: o["stack"].id,
    "session_id": lambda o: o["session"].id,
    "site_id": lambda o: o["site"].id,
    "role_id": lambda o: o["role"].id,
    "policy_id": lambda o: o["policy"].id,
    "playbook_id": lambda o: o["playbook"].id,
    "automation_id": lambda o: o["automation"].id,
    "report_id": lambda o: o["report"].id,
    "dashboard_id": lambda o: o["dashboard"].id,
    "job_id": lambda o: o["job"].id,
    "page_id": lambda o: o["page"].id,
    "finding_id": lambda o: o["finding"].id,
    # Both sites' SoftwareItem rows share this name_key (Spec 4): the two
    # requests are then identical, so the body can differ only if a site-B row leaks.
    "name_key": "shared-app",
    "pk": lambda o: o["webhook"].pk,
    # A container, a file, a package: filler with no site marker in it, so the
    # only difference between the two requests is the object id.
    "name": "nginx",
    "number": 1,
    "index": 0,
    "kind": "cpu",
    "category": "cpu",
    "metric_name": "cpu_pct",
    "filename": "task.yaml",
}

#: Routes deliberately not probed, each with a one-line reason.
ALLOWLIST = {
    "api/v1/health/": "unauthenticated liveness probe; carries no rows",
    "api/v1/about/": "build and edition metadata, identical for every site",
    "setup/": "first-run bootstrap, before any site exists",
    "login/": "public sign-in page",
    "login/verify/": "sign-in flow",
    "login/verify/key/options/": "sign-in flow",
    "login/verify/key/": "sign-in flow",
    "logout/": "sign-in flow",
    "accounts/civil/login/": "sign-in flow",
    "accounts/civil/callback": "sign-in flow",
    "api/v1/register": "agent-token auth",
    "api/v1/checkin": "agent-token auth",
    "api/v1/agent/log-tail/<uuid:session_id>/": "agent-token auth",
    "api/v1/agent/stack-env/<uuid:ticket_id>/": "agent-token auth",
    "api/v1/agent/stack-source/<uuid:ticket_id>/": "agent-token auth",
    "api/v1/agent/stack-adopt/<uuid:ticket_id>/": "agent-token auth",
    "api/v1/agent/registry-auth/": "agent-token auth",
    "api/v1/agent/wake": "agent-token auth",
    "api/v1/reprovision/enroll": "installer by one-time token",
    "reprovision/^answer/(?P<token>[\\w\\-]+)/?(?P<filename>[\\w\\-.]*)$": "installer by one-time token",
    "reprovision/^tree/(?P<image_id>[0-9a-fA-F\\-]+)/(?P<path>.*)$": "installer by one-time token",
    "agent/enroll-config/<str:token>/": "installer by one-time token",
    "agent/download/<str:platform>/": "agent binary, identical for every site",
    "agent/upload/<str:platform>/": "agent binary upload, identical for every site",
    "agent/install.sh": "installer script, identical for every site",
    "agent/install.ps1": "installer script, identical for every site",
    "agent/uninstall.sh": "installer script, identical for every site",
    "agent/uninstall.ps1": "installer script, identical for every site",
    "agent/info/": "the agent's own build inventory, not a site-scoped row",
    "status/<str:token>/": "public status page by token",
    "status/<str:token>/data/": "public status page by token",
    # Scoped by site in phase 01 and covered by its own test module.
    "api/v1/ai/providers/": "covered by apps/aisuggest/test_site_scope.py",
    "api/v1/ai/suggest/alert/<uuid:alert_id>/": "covered by apps/aisuggest/test_site_scope.py",
    "api/v1/ai/suggest/fix-group/": "covered by apps/aisuggest/test_site_scope.py",
    "api/v1/ai/suggest/vuln/<uuid:finding_id>/": "covered by apps/aisuggest/test_site_scope.py",
    # Instance-wide configuration: a user, an instance setting or a license
    # belongs to no site, so there is nothing another site could leak into it.
    "api/v1/accounts/users/": "instance-wide; a user belongs to no site",
    "api/v1/accounts/capabilities/": "the caller's own role",
    "api/v1/accounts/session/": "the caller's own session",
    "api/v1/accounts/session/activity/": "the caller's own session",
    "api/v1/accounts/totp/": "the caller's own second factor",
    "api/v1/accounts/totp/enroll/": "the caller's own second factor",
    "api/v1/accounts/totp/enroll/confirm/": "the caller's own second factor",
    "api/v1/accounts/totp/disable/": "the caller's own second factor",
    "api/v1/accounts/totp/debug-code/": "the caller's own second factor",
    "api/v1/accounts/security-keys/": "the caller's own second factor",
    "api/v1/accounts/security-keys/register/options/": "the caller's own second factor",
    "api/v1/accounts/security-keys/register/": "the caller's own second factor",
    "api/v1/accounts/security-keys/assert/options/": "the caller's own second factor",
    "api/v1/accounts/security-keys/assert/": "the caller's own second factor",
    "api/v1/instance-settings/": "instance-wide; one value shared by every site",
    "api/v1/instance-settings/test/": "instance-wide; one value shared by every site",
    "api/v1/license/": "instance-wide; one license for the whole install",
    "api/v1/license/refresh/": "instance-wide; one license for the whole install",
    "api/v1/civil/settings/": "instance-wide; one SSO tenant for the whole install",
    "api/v1/catalog/": "instance-wide; the upstream package catalog",
    "api/v1/catalog/categories/": "instance-wide; the upstream package catalog",
    "api/v1/catalog/status/": "instance-wide; the upstream package catalog",
    "api/v1/catalog/enable/": "instance-wide; the upstream package catalog",
    "api/v1/catalog/sync/": "instance-wide; the upstream package catalog",
    "api/v1/catalog/install-task/": "instance-wide; the upstream package catalog",
    "api/v1/tasks/community/": "instance-wide; the bundled community task library",
    "api/v1/tasks/community/<str:kind>/": "instance-wide; the community task library",
    "api/v1/tasks/community/<str:kind>/<str:filename>/fork/": "instance-wide; the community task library",
    "api/v1/tasks/community/<str:kind>/<str:filename>/plan/": "instance-wide; the community task library",
    "api/v1/reprovision/catalog/": "instance-wide; the reprovision image catalog",
    "api/v1/reprovision/images/pull/": "instance-wide; the reprovision image catalog",
    "api/v1/reprovision/images/upload/": "instance-wide; the reprovision image catalog",
    "api/v1/reprovision/images/": "instance-wide; the reprovision image catalog",
    "api/v1/reprovision/images/<uuid:image_id>/": "instance-wide; the reprovision image catalog",
    "api/v1/reprovision/preflight/": "reads a host by an agent token, not by a session",
    "api/v1/branding/": "instance-wide; one branding for the whole install",
    "api/v1/api-keys/": "instance-wide; a key is issued to a user, not a site",
    # The scope itself: these rows are what every site filter resolves against.
    "api/v1/sites/": "the scope itself; site rows are the boundary, not behind it",
    "api/v1/sites/<uuid:site_id>/": "the scope itself; site rows are the boundary, not behind it",
    "api/v1/sites/roles/": "the scope itself; a grant names a site, so it cannot hide one",
    "api/v1/sites/roles/<int:role_id>/": "the scope itself; a grant names a site, so it cannot hide one",
    # Instance-wide configuration with an id whose object no site can own.
    "api/v1/accounts/users/<int:user_id>/": "instance-wide; a user belongs to no site",
    "api/v1/accounts/users/<int:user_id>/role/": "instance-wide; a user belongs to no site",
    "api/v1/accounts/security-keys/<int:key_id>/": "the caller's own second factor",
    "api/v1/catalog/<int:package_id>/": "instance-wide; the upstream package catalog",
    "api/v1/catalog/<int:package_id>/icon/": "instance-wide; the upstream package catalog",
    "api/v1/api-keys/<int:pk>/revoke/": "instance-wide; a key is issued to a user, not a site",
    "api/v1/ai/providers/<int:provider_id>/": "instance-wide AI provider config",
    "api/v1/ai/providers/<int:provider_id>/test/": "instance-wide AI provider config",
    # Instance-wide by today's design: these rows carry no site. S1 phase 06
    # decides whether any of them should become per-site.
    "api/v1/tasks/definitions/<uuid:definition_id>/": "instance-wide; a task definition carries no site",
    "api/v1/tasks/definitions/<uuid:definition_id>/archive/": "instance-wide; a task definition carries no site",
    "api/v1/tasks/definitions/<uuid:definition_id>/fork/": "instance-wide; a task definition carries no site",
    "api/v1/tasks/definitions/<uuid:definition_id>/refusals/": "instance-wide; a task definition carries no site",
    "api/v1/tags/<int:tag_id>/": "instance-wide; a tag carries no site",
    "api/v1/relays/<uuid:relay_id>/": "instance-wide; a relay carries no site",
    "api/v1/relays/<uuid:relay_id>/rotate-key/": "instance-wide; a relay carries no site",
    "api/v1/reprovision/profiles/<uuid:profile_id>/": "instance-wide; an install profile carries no site",
}

#: Route -> {kwarg: filler} for routes whose kwarg name means a different
#: model than FILLERS assumes.
ROUTE_FILLERS = {
    "api/v1/hosts/log-tails/<uuid:session_id>/": {"session_id": lambda o: o["logtail"].id},
}

#: Routes whose kwargs have no filler yet (phase 02b covers them). Pinned exactly:
#: a stale entry fails, so does a new route that is in neither table.
NOT_YET_COVERED = {
    "api/v1/agent-profiles/<uuid:profile_id>/",
    "api/v1/hosts/devices/<uuid:device_id>/",
    "api/v1/jackil/integration-keys/<int:pk>/devices/",
    "api/v1/jackil/integration-keys/<int:pk>/revoke/",
    "api/v1/jackil/integration/<str:kind>/<str:id>/",
    "api/v1/jackil/integration/devices/<str:id>/",
    "api/v1/jackil/integration/devices/<uuid:id>/",
    "api/v1/playbooks/library/<str:template_id>/use/",
    "api/v1/reprovision/profiles/<uuid:profile_id>/preview/",
    "api/v1/rollouts/<uuid:rollout_id>/",
    "api/v1/rollouts/<uuid:rollout_id>/halt/",
    "api/v1/rollouts/<uuid:rollout_id>/resume/",
    "api/v1/rollouts/<uuid:rollout_id>/skip-validation/",
    "api/v1/rollouts/<uuid:rollout_id>/waves/<int:wave_id>/hosts/",
    "api/v1/rollouts/batch/<uuid:batch>/halt/",
    "api/v1/stacks/git-credentials/<uuid:cred_id>/",
    "api/v1/stacks/registries/<uuid:cred_id>/",
    "api/v1/tasks/runs/<uuid:run_id>/hunt/",
    "api/v1/waves/<int:wave_id>/",
    # Deploy names its target hosts in the body, not the URL.
    "api/v1/tasks/definitions/<uuid:definition_id>/deploy/",
    # A run spans hosts; it is visible when one of its hosts is.
    "api/v1/tasks/runs/<uuid:run_id>/",
}

#: "<METHOD> <route>" -> what was observed. Pinned exactly. Later phases fix
#: these and delete the line; an empty dict is the milestone's finish line.
KNOWN_LEAKS: dict[str, str] = {
}

_KWARG = re.compile(r"<(?:(\w+):)?(\w+)>")


class UnfillableRoute(Exception):
    """A route reached by the walk whose kwargs FILLERS cannot supply — always
    a bug in this module: the route belongs in NOT_YET_COVERED or FILLERS."""


def all_routes():
    """[(route_string, callback)] for every URL pattern, e.g.
    ("api/v1/hosts/<uuid:host_id>/firewall/", <function>)."""
    out = []

    def walk(resolver, prefix=""):
        for p in resolver.url_patterns:
            if isinstance(p, URLResolver):
                walk(p, prefix + str(p.pattern))
            else:
                out.append((prefix + str(p.pattern), p.callback))

    walk(get_resolver())
    return out


def is_admin_route(route):
    # Django's own admin, which dana cannot reach: she is not staff, so every
    # one of these answers with a redirect that says nothing about sites.
    return route == "admin/" or route.startswith("admin/")


def routes_to_probe():
    """Routes this walk covers: no admin, no reason not to probe, and no regex —
    a regex route's string cannot be filled with `re.sub`, so they are allowlisted."""
    return [
        (route, callback)
        for route, callback in all_routes()
        if not is_admin_route(route)
        and "^" not in route
        and route not in ALLOWLIST
        and route not in NOT_YET_COVERED
        and all(name in FILLERS or name in ROUTE_FILLERS.get(route, {})
                for _, name in _KWARG.findall(route))
    ]


def unfillable(routes):
    """Routes with a kwarg FILLERS cannot supply and no table names."""
    out = []
    for route, _ in routes:
        if is_admin_route(route) or "^" in route:
            continue
        if route in ALLOWLIST or route in NOT_YET_COVERED:
            continue
        missing = [name for _, name in _KWARG.findall(route)
                   if name not in FILLERS and name not in ROUTE_FILLERS.get(route, {})]
        if missing:
            out.append((route, missing))
    return out


def verb_set(callback):
    """The verbs a callback really accepts, among get/post/put/patch/delete.

    A DRF view (``@api_view`` or a class) lists them in
    ``cls.http_method_names``; OPTIONS/HEAD are not probes. A view with no
    such list is a plain Django view, which accepts what an HTML form can
    send: get/post."""
    cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
    if cls is None:
        return ["get", "post"]
    names = getattr(cls, "http_method_names", None) or []
    return sorted({v.lower() for v in names} & {"get", "post", "put", "patch", "delete"})


def probe_methods_for(callback):
    """Every verb the route accepts. Writes are rolled back by ``probe``, and
    the Django test client does not enforce CSRF, so a write that wrongly
    reaches a site-B object shows up as a status that differs from the
    absent-object one."""
    return verb_set(callback)


#: The id that stands for "an object that does not exist" when a kwarg is an
#: integer primary key.
ABSENT_INT = 2_000_000_000


def _fill(route, objs, absent=False):
    """The route as a concrete URL, kwargs from ``objs``. With ``absent``, every
    object id becomes one that does not exist (a fresh uuid4, or ABSENT_INT);
    plain string and int fillers keep their value, which is what makes the two
    responses comparable at all."""
    def repl(match):
        name = match.group(2)
        filler = ROUTE_FILLERS.get(route, {}).get(name, FILLERS.get(name))
        if filler is None:
            raise LookupError(name)
        if not callable(filler):
            return str(filler)
        value = filler(objs)
        if absent and isinstance(value, uuid.UUID):
            value = uuid.uuid4()
        elif absent and isinstance(value, int):
            value = ABSENT_INT
        elif absent:
            value = f"absent-{uuid.uuid4().hex[:8]}"
        return str(value)

    return "/" + _KWARG.sub(repl, route)


def probe(client, method, url):
    """One request, rolled back: a DELETE or write that wrongly succeeds on
    site B must not change the fixture for later probes."""
    with transaction.atomic():
        try:
            if method == "get":
                resp = client.get(url)
            elif method == "delete":
                resp = client.delete(url)
            else:
                resp = getattr(client, method)(
                    url, data="{}", content_type="application/json"
                )
        finally:
            transaction.set_rollback(True)
    return resp


def body_of(resp):
    return b"".join(resp.streaming_content) if resp.streaming else resp.content


def leaks_for(client, objs, route, callback):
    """{"<METHOD> <route>": observation} for every way this route can leak.

    With URL kwargs: a site-B object must be indistinguishable from an object
    that does not exist — same status code, and no site-B text in the body.
    Without kwargs: no site-B row may appear in the listing at all."""
    try:
        url_b = _fill(route, objs)
        url_x = _fill(route, objs, absent=True)
    except LookupError as exc:
        raise UnfillableRoute(f"{route} needs a filler for {exc}") from exc

    # A route with no kwargs is a collection: GET only, whatever it accepts.
    methods = probe_methods_for(callback) if _KWARG.findall(route) else ["get"]

    found = {}
    for method in methods:
        resp_b = probe(client, method, url_b)
        resp_x = probe(client, method, url_x)
        if resp_b.status_code != resp_x.status_code:
            found[f"{method.upper()} {route}"] = (
                f"{resp_b.status_code} vs {resp_x.status_code}"
            )
        elif LEAK_MARKER.encode() in body_of(resp_b).lower():
            found[f"{method.upper()} {route}"] = f"{LEAK_MARKER} in body"
    return found


@api_view(["GET"])
@permission_classes([AllowAny])
def leaky_host(request, host_id):
    """Deliberately unscoped, for test_the_oracle_catches_a_leak: a view that
    hands back any host by id, which is exactly the bug this module hunts."""
    # Deliberately unscoped: since 04b the default manager would hide it.
    host = Host.objects.unscoped(reason="the oracle needs a real leak").get(pk=host_id)
    return Response({"hostname": host.hostname})


#: Just the one route, for override_settings(ROOT_URLCONF=__name__).
urlpatterns = [django_path("leaky/<uuid:host_id>/", leaky_host)]


#: A throwaway signing key, so the walk runs with every Business feature on.
#: Unlicensed, each Business write answers 402 for site B and for a missing
#: object alike, and the walk cannot tell a leak from a paywall.
_LICENSE_KEY = SigningKey.generate()


def _business_license():
    claims = {"instance": licensing.instance_id(), "org": "isolation-test", "seats": 50,
              "sites": None, "exp": int(time.time()) + 86400, "iat": int(time.time())}
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()

    def b64u(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    return f"{licensing.PREFIX}.{b64u(payload)}.{b64u(_LICENSE_KEY.sign(payload).signature)}"


@override_settings(VIGIL_LICENSE_PUBLIC_KEY=base64.b64encode(
    _LICENSE_KEY.verify_key.encode()).decode())
class SiteIsolationTests(TestCase):
    """Dana administers West Campus only. For every route she can reach, a Lab
    object must be indistinguishable from an object that does not exist."""

    @classmethod
    def setUpTestData(cls):
        cls.west = Site.objects.create(name="West Campus", slug="west-campus")
        cls.lab = Site.objects.create(name="Lab", slug="lab")
        cls.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=cls.dana, site=cls.west, role=Role.ADMIN)

        cls.objs = {}
        for tag, site in ((MINE, cls.west), (THEIRS, cls.lab)):
            o = {"site": site}
            # Instance-wide rows (rules, tags, relays, images, definitions,
            # waves) belong to no site, so they are named without the marker:
            # a listing that shows them to every site is correct, not a leak.
            shared = "one" if tag == MINE else "two"
            # Site-owned rows that a *user* owns (dashboards, reports) belong to
            # a member of that site, never to dana.
            member = get_user_model().objects.create_user(f"{tag}-member", password="x")

            o["host"] = Host.objects.create(
                hostname=f"{tag}-host", agent_token=f"tok-{tag}"
            )
            HostSiteAssignment.objects.create(host=o["host"], site=site)

            rule = AlertRule.objects.create(
                name=f"shared-{shared}-rule",
                category="cpu",
                metric="cpu",
                operator="gt",
                threshold=90,
                severity="warning",
            )
            o["alert"] = Alert.objects.create(
                host=o["host"], rule=rule, severity="warning", message=f"{tag} alert"
            )

            o["finding"] = VulnFinding.objects.create(
                host=o["host"],
                title=f"{tag} finding",
                scanner="trivy",
                plugin_id_or_oid=f"{tag}:CVE-1",
                cve_id="CVE-2024-0001",
                severity="high",
                package_name=f"{tag}-pkg",
            )

            # Time series. The category carries the marker so the fleet-wide
            # metric catalog shows a leak if it ever lists another site's.
            MetricPoint.objects.create(host=o["host"], time=timezone.now(),
                                       category=f"{tag}-cat", metric="usage_percent", value=1)
            HostUptimeSample.objects.create(host=o["host"], time=timezone.now(), up=True)
            VulnScoreHistory.objects.create(host=o["host"], date=timezone.localdate(), score=7)

            o["definition"] = TaskDefinition.objects.create(
                name=f"shared-{shared}-definition",
                risk_level="standard",
                yaml_source="",
                parsed_spec={"risk": "standard", "actions": []},
            )
            o["task"] = Task.objects.create(
                host=o["host"],
                action="run_command",
                nonce=f"{tag}-nonce",
                risk_level=Task.RiskLevel.STANDARD,
                state=Task.State.PENDING,
            )
            o["run"] = TaskRun.objects.create(
                name_snapshot=f"shared-{shared} run", requested_by=cls.dana
            )

            o["stack"] = ManagedStack.objects.create(
                host=o["host"], name=f"{tag}-stack", compose_yaml="services: {}\n"
            )
            o["registry_cred"] = RegistryCredential.objects.create(
                registry=f"registry.{tag}.example", username=tag, password_encrypted=b"",
                site_id=site.pk,
            )
            o["git_cred"] = GitCredential.objects.create(
                name=f"{tag}-git", kind=GitCredential.Kind.TOKEN, secret_encrypted=b"",
                site_id=site.pk,
            )
            o["logtail"] = LogTailSession.objects.create(
                host=o["host"], container_name=f"{tag}-container", requested_by=member)
            StackRevision.objects.create(
                stack=o["stack"], number=1, compose_yaml="services: {}\n",
                note=f"{tag} revision")

            o["session"] = RemoteSession.objects.create(
                host=o["host"],
                username=cls.dana.username,
                kind=RemoteSession.Kind.SHELL,
                nonce=f"{tag}-snonce",
                state=RemoteSession.State.CLOSED,
            )

            o["playbook"] = Playbook.objects.create(name=f"{tag}-playbook")
            PlaybookSiteAssignment.objects.create(playbook=o["playbook"], site=site)

            o["automation"] = Automation.objects.create(
                name=f"{tag}-automation",
                trigger=Automation.Trigger.SCHEDULE,
                action_kind=Automation.ActionKind.TASK,
                task_definition=o["definition"],
                target=Automation.Target.ALL,
            )
            AutomationSiteAssignment.objects.create(automation=o["automation"], site=site)
            o["dashboard"] = Dashboard.objects.create(owner=member, name=f"{tag}-dash")
            o["report"] = Report.objects.create(name=f"{tag}-report", owner=member)
            o["relay"] = Relay.objects.create(name=f"shared-{shared}-relay")
            # Per site since 04d-1 (decisions-06 item 5).
            o["profile"] = AgentProfile.objects.create(name=f"{tag}-profile", site_id=site.pk)
            o["device"] = UnmanagedDevice.objects.create(name=f"{tag}-device", site_id=site.pk)
            o["page"] = StatusPage.objects.create(title=f"{tag}-page", site_id=site.pk)
            o["key"] = IntegrationKey.objects.create(
                name=f"shared-{shared}-key",
                prefix="ky",
                key_hash=f"shared-{shared}-hash",
                created_by=cls.dana,
            )

            policy = UpdatePolicy.objects.create(name=f"{tag}-policy", site_id=site.pk)
            policy.save()
            o["policy"] = policy
            o["change"] = PolicyChange.objects.create(
                policy=policy, host=o["host"], changes=[{"app": f"{tag}-app"}]
            )

            o["image"] = OSImage.objects.create(
                name=f"shared-{shared}-image",
                os_family=OSImage.Family.UBUNTU,
                version="1.0",
                sha256="0" * 64,
            )
            o["install_profile"] = InstallProfile.objects.create(
                name=f"{tag}-install",
                image=o["image"],
                disk_target="/dev/sda",
                admin_username=tag,
                site_id=site.pk,
                admin_password_encrypted=b"",
            )
            o["job"] = RebuildJob.objects.create(
                host=o["host"],
                image=o["image"],
                profile=o["install_profile"],
                deadline=timezone.now() + timedelta(hours=1),
            )

            o["wave"] = PatchWave.objects.create(name=f"shared-{shared}-wave", order=1)

            SoftwareItem.objects.create(
                host=o["host"],
                source=SoftwareItem.Source.DPKG,
                package_id=f"{tag}-pkg",
                name=f"{tag}-app",
                name_key="shared-app",
                version="1.0",
                first_seen=timezone.now(),
            )

            o["tag"] = Tag.objects.create(name=f"shared-{shared}-tag")
            o["host"].tags = [o["tag"].name]
            o["host"].save()

            # A grant id for the walk to probe. It belongs to another user:
            # giving dana a row on the Lab site would make Lab hers to see.
            o["role"] = UserSiteRole.objects.create(
                user=member, site=site, role=Role.OPERATOR
            )

            o["webhook"] = Webhook.objects.create(
                name=f"{tag}-hook", url=f"https://hooks.example.com/{tag}",
                events=["alert.sent"], created_by=member)
            o["webhook"].sites.set([site])

            cls.objs[tag] = o

    def setUp(self):
        self.client.raise_request_exception = False
        self.client.force_login(self.dana)
        licensing.reload()
        licensing.set_license(_business_license())
        self.assertTrue(licensing.has_feature("sites"), "the walk must run licensed")

    def tearDown(self):
        from apps.licensing.models import StoredLicense
        StoredLicense.replace("")
        licensing.reload()

    # -- the four tests ----------------------------------------------------

    def test_every_route_is_classified(self):
        for route, reason in ALLOWLIST.items():
            self.assertTrue(reason.strip(), f"{route} is allowlisted with no reason")
        routes = all_routes()
        missing = unfillable(routes)
        self.assertEqual(
            missing, [],
            "routes with kwargs FILLERS cannot supply that appear in neither "
            f"ALLOWLIST nor NOT_YET_COVERED: {missing}")
        known = {route for route, _ in routes}
        for table, entries in (
            ("ALLOWLIST", ALLOWLIST),
            ("NOT_YET_COVERED", NOT_YET_COVERED),
        ):
            stale = sorted(e for e in entries if e not in known)
            self.assertEqual(stale, [], f"stale {table} entries: {stale}")

    def test_no_route_leaks_site_b(self):
        with mock.patch(
            "socket.socket.connect",
            side_effect=OSError("no network in the isolation test"),
        ):
            found = {}
            for route, callback in routes_to_probe():
                found.update(leaks_for(self.client, self.objs[THEIRS], route, callback))
        self.maxDiff = None
        new = {k: v for k, v in found.items() if k not in KNOWN_LEAKS}
        fixed = {k: v for k, v in KNOWN_LEAKS.items() if k not in found}
        self.assertEqual(
            sorted(found),
            sorted(KNOWN_LEAKS),
            f"NEW leaks (pin them only if they are not bugs): {new}\n"
            f"FIXED leaks (delete these lines from KNOWN_LEAKS): {fixed}",
        )

    def test_positive_control(self):
        """The login and the fixture work, so an all-404 walk is not a pass.
        Also asserts the oracle's premise for a scoped collection: what dana
        sees, she sees as a bare list."""
        resp = self.client.get(f"/api/v1/hosts/{self.objs[MINE]['host'].id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn(b"west-host", body_of(resp))
        listing = self.client.get("/api/v1/hosts/")
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertIn(b"west-host", body_of(listing))
        # A scoped collection answers a bare list; a paginated one would hide a
        # leak from the walk's body scan.
        self.assertIsInstance(listing.json(), list, "the walk assumes no pagination")

    def test_the_oracle_catches_a_leak(self):
        """A deliberately unscoped endpoint must make the walk fail — the proof
        that the ratchet ratchets."""
        with override_settings(ROOT_URLCONF=__name__):
            found = leaks_for(
                self.client, self.objs[THEIRS], "leaky/<uuid:host_id>/", leaky_host
            )
        self.assertIn("GET leaky/<uuid:host_id>/", found,
                      f"the oracle missed the leak: {found}")
