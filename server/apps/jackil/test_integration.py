"""The key's own tests: what it can open, what it cannot, and what it must never see.

The security-relevant assertions here are the negative ones — a key refused
outside its five endpoints, a session refused inside them, and a cross-site
row that none of them will hand over.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now, timedelta
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.alerts.models import Alert, AlertRule
from apps.automations.models import Automation
from apps.hosts.models import Host
from apps.jackil.models import KEY_PREFIX, IntegrationKey, JackilTicket
from apps.playbooks.models import Playbook
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import parse_and_validate
from vigil.testsupport import require_business_module

from .integration_views import _FINDERS, TYPES

CHECK = ("name: Check cron\nactions:\n  - id: c\n    type: check_service\n"
         "    params:\n      service_name: cron\n")

PING = "/api/v1/jackil/integration/ping/"
SEARCH = "/api/v1/jackil/integration/search/"
RESOLVE = "/api/v1/jackil/integration/resolve/"
DETAIL = "/api/v1/jackil/integration/{}"
KEYS = "/api/v1/jackil/integration-keys/"

#: A primary key no row has ever had, for the "unknown id" side of every
#: indistinguishability assertion.
UNKNOWN_HOST = "00000000-0000-0000-0000-000000000000"


def _admin(name="admin"):
    return get_user_model().objects.create_superuser(username=name, email=f"{name}@x",
                                                     password="pw")


def _key(name="desk", user=None):
    return IntegrationKey.mint(name=name, user=user or _admin())


def _authed(raw=None, user=None):
    """A client carrying an integration key. With ``user`` it mints a fresh one
    for them, which is how a test says "this key belongs to Dana"."""
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw or _key(user=user)[1]}")
    return client


def _detail(kind, pk):
    return DETAIL.format(f"{kind}/{pk}/")


class MintTests(TestCase):
    def test_mint_stores_only_a_hash(self):
        key, raw = _key(name="Front desk")
        key.refresh_from_db()
        self.assertTrue(raw.startswith(KEY_PREFIX))
        self.assertEqual(key.prefix, raw[len(KEY_PREFIX):len(KEY_PREFIX) + 8])
        for field in IntegrationKey._meta.fields:
            value = getattr(key, field.attname)
            if isinstance(value, str):
                self.assertNotIn(raw, value, f"{field.name} holds the plaintext")

    def test_two_keys_are_two_plaintexts_and_two_rows(self):
        admin = _admin()
        first, first_raw = _key(name="a", user=admin)
        second, second_raw = _key(name="b", user=admin)
        self.assertNotEqual(first_raw, second_raw)
        self.assertNotEqual(first.key_hash, second.key_hash)
        self.assertEqual(IntegrationKey.objects.count(), 2)


class PingTests(TestCase):
    def test_ping_with_a_valid_key(self):
        client = _authed(user=_admin("desk-admin"))
        response = client.get(PING)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], settings.VIGIL_VERSION)
        self.assertEqual(body["acting_as"], "desk-admin")

    def test_missing_unknown_revoked_and_inactive_creator_are_401(self):
        admin = _admin()
        client = APIClient()
        self.assertEqual(client.get(PING).status_code, 401, "no header at all")

        client.credentials(HTTP_AUTHORIZATION="Bearer vjk_never-minted-xxxxxxxxxxxxxxxxx")
        self.assertEqual(client.get(PING).status_code, 401, "a key nobody minted")

        _, raw = _key(user=admin)
        key = IntegrationKey.objects.get()
        key.revoked_at = now()
        key.save(update_fields=["revoked_at"])
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(client.get(PING).status_code, 401, "a revoked key")

        _, raw = _key(user=admin)
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = client.get(PING)
        self.assertEqual(response.status_code, 401, "whose creator was disabled")
        self.assertEqual(response.json(), {"detail": "Invalid integration key"})

    def test_a_foreign_credential_is_401_not_500(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer sha256$deadbeef")
        self.assertEqual(client.get(PING).status_code, 401)

    def test_another_prefixes_bearer_token_is_passed_on_not_rejected_here(self):
        """A credential that is not ours is not this authenticator's business:
        it falls through to the session auth in DRF's defaults, which answers
        as it would for any unauthenticated request. Ours is refused outright
        so a rejected key cannot borrow a session."""
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + "k" * 64)
        self.assertEqual(client.get(PING).status_code, 401)

    def test_last_used_is_throttled(self):
        key, raw = _key(user=_admin())
        self.assertIsNone(key.last_used_at)
        client = _authed(raw)

        client.get(PING)
        key.refresh_from_db()
        self.assertIsNotNone(key.last_used_at)
        recorded = key.last_used_at

        # Ten seconds later: authenticated again, but not re-recorded.
        IntegrationKey.objects.update(last_used_at=recorded - timedelta(seconds=10))
        self.assertEqual(client.get(PING).status_code, 200)
        key.refresh_from_db()
        self.assertEqual(key.last_used_at, recorded - timedelta(seconds=10))

        # Two minutes on: due again.
        stale = now() - timedelta(minutes=2)
        IntegrationKey.objects.update(last_used_at=stale)
        client.get(PING)
        key.refresh_from_db()
        self.assertGreater(key.last_used_at, stale)


class CredentialIsolationTests(TestCase):
    """Neither credential may use the other's doors."""

    def setUp(self):
        self.admin = _admin("boss")
        _, self.raw = _key(user=self.admin)
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32)

    def test_key_is_refused_by_every_other_api(self):
        client = _authed(self.raw)
        for url in ("/api/v1/hosts/", "/api/v1/jackil/tickets/", KEYS):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertIn(response.status_code, (401, 403),
                              f"{url} -> {response.status_code}")

    def test_session_is_refused_by_integration_endpoints(self):
        client = APIClient()
        client.force_login(self.admin)
        for url in (PING, SEARCH, RESOLVE, _detail("host", self.host.id)):
            with self.subTest(url=url):
                self.assertIn(client.get(url).status_code, (401, 403))

    def test_only_get_is_allowed(self):
        client = _authed(self.raw)
        for method in ("post", "put", "patch", "delete"):
            for url in (PING, SEARCH, RESOLVE, _detail("host", self.host.id)):
                with self.subTest(method=method, url=url):
                    response = getattr(client, method)(url, {}, format="json")
                    self.assertIn(response.status_code, (401, 403, 405),
                                  f"{method.upper()} {url} -> {response.status_code}")


class SearchTests(TestCase):
    def setUp(self):
        self.admin = _admin("boss")
        self.client = _authed(user=self.admin)
        self.host = Host.objects.create(
            hostname="db-prod-01", agent_token="t" * 32, os="Ubuntu 24.04",
            ip_address="10.2.1.7", status=Host.Status.ONLINE, tags=["prod", "db"])
        self.rejected = Host.objects.create(hostname="db-prod-rejected",
                                            agent_token="u" * 32,
                                            status=Host.Status.REJECTED)
        self.definition = TaskDefinition.objects.create(
            owner=self.admin, name="Check cron", description="Is cron running",
            yaml_source=CHECK, parsed_spec=parse_and_validate(CHECK))
        self.playbook = Playbook.objects.create(name="Nightly scan", description="d")
        self.automation = Automation.objects.create(
            name="Nightly automation", trigger=Automation.Trigger.SCHEDULE,
            action_kind=Automation.ActionKind.TASK, task_definition=self.definition,
            target=Automation.Target.ALL)
        self.run = TaskRun.objects.create(name_snapshot="Nightly scan run", host_count=3)
        Task.objects.create(host=self.host, run=self.run, action="check_service",
                            nonce="n1", params={})

    def _types(self, results):
        return sorted({row["type"] for row in results})

    def test_search_each_type(self):
        self.assertEqual(self._types(self.client.get(SEARCH, {"q": "nightly"}).json()["results"]),
                         ["automation", "playbook", "run"])
        for row in self.client.get(SEARCH, {"q": "db-prod"}).json()["results"]:
            self.assertEqual(row["type"], "host")

    def test_host_search_matches_os_and_tags_like_the_page(self):
        for q in ("Ubuntu", "prod"):
            with self.subTest(q=q):
                ids = [r["id"] for r in self.client.get(SEARCH, {"q": q}).json()["results"]]
                self.assertIn(str(self.host.id), ids)

    def test_rejected_hosts_are_never_returned(self):
        ids = [r["id"] for r in self.client.get(SEARCH, {"q": "db-prod"}).json()["results"]]
        self.assertNotIn(str(self.rejected.id), ids)

    def test_item_shape(self):
        row = self.client.get(SEARCH, {"q": "db-prod"}).json()["results"][0]
        self.assertEqual(sorted(row), ["id", "label", "status", "sub", "tone", "type", "url"])
        self.assertEqual((row["label"], row["sub"], row["status"], row["tone"]),
                         ("db-prod-01", "Ubuntu 24.04 · 10.2.1.7", "Online", "ok"))
        self.assertTrue(row["url"].endswith(f"#open/host/{self.host.id}"), row["url"])

    def test_run_item_names_the_run_and_counts_its_hosts(self):
        row = self.client.get(SEARCH, {"q": "Nightly scan run"}).json()["results"][0]
        self.assertEqual((row["label"], row["status"], row["tone"]),
                         ("Nightly scan run", "Running", "info"))
        self.assertTrue(row["sub"].endswith("3 hosts"), row["sub"])

    def test_single_host_is_not_pluralised(self):
        TaskRun.objects.create(name_snapshot="Solo run", host_count=1)
        row = self.client.get(SEARCH, {"q": "Solo run"}).json()["results"][0]
        self.assertTrue(row["sub"].endswith("1 host"), row["sub"])

    def test_short_query_returns_nothing(self):
        for q in ("d", "  ", None):
            with self.subTest(q=q):
                params = {} if q is None else {"q": q}
                self.assertEqual(self.client.get(SEARCH, params).json(), {"results": []})

    def test_type_narrows_and_unknown_type_is_400(self):
        results = self.client.get(SEARCH, {"q": "nightly", "type": "playbook"}).json()["results"]
        self.assertEqual(self._types(results), ["playbook"])
        response = self.client.get(SEARCH, {"q": "nightly", "type": "container"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Unknown type"})

    def test_at_most_eight_per_type(self):
        for n in range(12):
            Host.objects.create(hostname=f"db-prod-{n + 50}",
                                agent_token=f"tok-{n}-{'x' * 22}",
                                status=Host.Status.ONLINE)
        results = self.client.get(SEARCH, {"q": "db-prod"}).json()["results"]
        self.assertEqual(len([r for r in results if r["type"] == "host"]), 8)


class ResolveTests(TestCase):
    def setUp(self):
        self.client = _authed(user=_admin("boss"))
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32,
                                        status=Host.Status.ONLINE)
        self.other = Host.objects.create(hostname="web2", agent_token="u" * 32)
        self.count = Automation.objects.create(
            name="Nightly", trigger=Automation.Trigger.SCHEDULE,
            action_kind=Automation.ActionKind.PLAYBOOK, playbook=None)

    def test_resolve_returns_the_rows_keyed_by_id(self):
        body = self.client.get(RESOLVE, {"type": "host",
                                         "ids": f"{self.host.id}, {self.other.id}"}).json()
        self.assertEqual(sorted(body["items"]), sorted([str(self.host.id), str(self.other.id)]))
        self.assertEqual(body["items"][str(self.host.id)]["label"], "web1")

    def test_malformed_and_missing_ids_are_absent_never_an_error(self):
        body = self.client.get(RESOLVE, {"type": "host",
                                         "ids": f"not-a-uuid,{self.host.id},,"}).json()
        self.assertEqual(list(body["items"]), [str(self.host.id)])
        self.assertEqual(self.client.get(RESOLVE, {"type": "host",
                                                   "ids": UNKNOWN_HOST}).json(),
                         {"items": {}})

    def test_an_int_primary_key_is_not_confused_with_a_uuid(self):
        # A UUID-shaped id asked of an int-keyed kind, and vice versa: both are
        # absent, neither is a 500 from a field that cannot hold it.
        self.assertEqual(self.client.get(RESOLVE, {"type": "automation",
                                                   "ids": str(self.host.id)}).json(),
                         {"items": {}})
        self.assertEqual(self.client.get(RESOLVE, {"type": "host", "ids": "7"}).json(),
                         {"items": {}})

    def test_more_than_a_hundred_ids_is_400(self):
        response = self.client.get(RESOLVE, {"type": "host",
                                             "ids": ",".join([str(self.host.id)] * 101)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("100", response.json()["error"])
        # Exactly a hundred is fine.
        ok = self.client.get(RESOLVE, {"type": "host",
                                       "ids": ",".join([str(self.host.id)] * 100)})
        self.assertEqual(ok.status_code, 200)

    def test_unknown_or_missing_type_is_400(self):
        self.assertEqual(self.client.get(RESOLVE, {"type": "nope", "ids": "1"}).status_code, 400)
        self.assertEqual(self.client.get(RESOLVE, {"ids": "1"}).status_code, 400)


class DetailTests(TestCase):
    def setUp(self):
        self.owner = _admin("owner")
        self.client = _authed(user=self.owner)
        self.host = Host.objects.create(hostname="web1", agent_token="t" * 32,
                                        os="Debian 13", ip_address="10.0.0.5",
                                        status=Host.Status.OFFLINE)
        self.definition = TaskDefinition.objects.create(
            owner=self.owner, name="Check cron", risk_level="high",
            yaml_source=CHECK, parsed_spec=parse_and_validate(CHECK))
        self.playbook = Playbook.objects.create(name="Nightly", target_tags=["web", "db"])
        self.automation = Automation.objects.create(
            name="Nightly automation", trigger=Automation.Trigger.EVENT,
            event="alert_fired", action_kind=Automation.ActionKind.TASK,
            task_definition=self.definition, enabled=False, target=Automation.Target.ALL)
        self.run = TaskRun.objects.create(name_snapshot="Nightly run", host_count=2,
                                          state=TaskRun.State.FAILED)

    def _facts(self, url):
        body = self.client.get(url).json()
        return {row["label"]: row["value"] for row in body["facts"]}, body

    def test_each_type_carries_its_facts(self):
        expected = {
            _detail("host", self.host.id): {
                "OS": "Debian 13", "IP": "10.0.0.5", "Status": "Offline"},
            _detail("task", self.definition.id): {"Risk": "High", "Owner": "owner"},
            _detail("playbook", self.playbook.id): {"Steps": "0", "Target tags": "web, db"},
            _detail("automation", self.automation.id): {
                "Trigger": "When an event fires", "Action": "Check cron", "Enabled": "No"},
            _detail("run", self.run.id): {"State": "Failed", "Hosts": "2"},
        }
        for url, want in expected.items():
            with self.subTest(url=url):
                facts, body = self._facts(url)
                for label, value in want.items():
                    self.assertEqual(facts.get(label), value, f"{url} → {facts}")
                self.assertTrue(all(isinstance(f["value"], str) for f in body["facts"]))

    def test_owner_is_the_definitions_user(self):
        facts, _ = self._facts(_detail("task", self.definition.id))
        self.assertEqual(facts["Owner"], "owner")

    def test_empty_facts_are_omitted_not_blank(self):
        facts, _ = self._facts(_detail("host", self.host.id))
        self.assertNotIn("Last seen", facts)
        self.assertEqual(sorted(facts), ["IP", "OS", "Status"])

    def test_missing_and_malformed_answer_identically(self):
        missing = self.client.get(_detail("host", UNKNOWN_HOST))
        malformed = self.client.get(_detail("host", "not-a-uuid"))
        for response in (missing, malformed):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"error": "Not found"})
        self.assertEqual(missing.content, malformed.content)

    def test_unknown_type_is_400(self):
        response = self.client.get(_detail("container", self.host.id))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Unknown type"})


class KeyManagementTests(TestCase):
    def setUp(self):
        self.admin = _admin("boss")
        self.operator = get_user_model().objects.create_user("oper", password="pw")
        UserProfile.objects.create(user=self.operator, role=Role.OPERATOR)
        self.client = APIClient()

    def _login(self, user):
        self.client.force_login(user)
        return self.client

    def test_operator_is_refused_the_key_pages(self):
        self._login(self.operator)
        self.assertEqual(self.client.get(KEYS).status_code, 403)
        self.assertEqual(self.client.post(KEYS, {"name": "x"}, format="json").status_code, 403)

    def test_plaintext_appears_once(self):
        self._login(self.admin)
        created = self.client.post(KEYS, {"name": "Front desk"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertTrue(body["key"].startswith(KEY_PREFIX))
        self.assertEqual(body["prefix"], body["key"][len(KEY_PREFIX):len(KEY_PREFIX) + 8])
        self.assertEqual(body["created_by"], "boss")
        self.assertIsNone(body["last_used_at"])
        self.assertIsNone(body["revoked_at"])

        listed = self.client.get(KEYS).json()["keys"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("key", listed[0])
        self.assertNotIn("key_hash", listed[0])

        revoked = self.client.post(f"{KEYS}{listed[0]['id']}/revoke/", {}, format="json")
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertIsNotNone(revoked.json()["revoked_at"])
        self.assertNotIn("key", revoked.json())

    def test_a_minted_key_works_immediately(self):
        self._login(self.admin)
        raw = self.client.post(KEYS, {"name": "desk"}, format="json").json()["key"]
        self.assertEqual(_authed(raw).get(PING).status_code, 200)

    def test_name_is_required_and_bounded(self):
        self._login(self.admin)
        for name in ("", "   ", "x" * 81):
            with self.subTest(length=len(name)):
                response = self.client.post(KEYS, {"name": name}, format="json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(IntegrationKey.objects.count(), 0)

    def test_an_unscoped_admin_sees_every_key_and_their_list_is_scoped(self):
        peer = _admin("other-admin")
        _key(name="theirs", user=peer)
        self._login(self.admin)
        names = [k["name"] for k in self.client.get(KEYS).json()["keys"]]
        self.assertEqual(names, ["theirs"])

    def test_revoking_something_invisible_is_404(self):
        self._login(self.admin)
        self.assertEqual(self.client.post(f"{KEYS}999999/revoke/", {},
                                          format="json").status_code, 404)

    def test_revoking_twice_is_what_a_double_click_does(self):
        key, _ = _key(user=self.admin)
        self._login(self.admin)
        first = self.client.post(f"{KEYS}{key.pk}/revoke/", {}, format="json")
        second = self.client.post(f"{KEYS}{key.pk}/revoke/", {}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["revoked_at"], second.json()["revoked_at"])

    def test_a_key_cannot_read_the_key_list(self):
        _, raw = _key(user=self.admin)
        self.assertIn(_authed(raw).get(KEYS).status_code, (401, 403))


class ReadOnlyTests(TestCase):
    """A key is a read grant: apart from the throttled ``last_used_at``, no
    endpoint may write anything."""

    def test_no_row_is_created_or_changed_by_reading(self):
        host = Host.objects.create(hostname="web1", agent_token="t" * 32,
                                   status=Host.Status.ONLINE)
        rule = AlertRule.objects.create(name="cpu", category="cpu", metric="cpu",
                                        operator="gt", threshold=90, severity="warning")
        alert = Alert.objects.create(host=host, rule=rule, severity="warning", message="m")
        run = TaskRun.objects.create(name_snapshot="r", host_count=1)
        Task.objects.create(host=host, run=run, action="check_service", nonce="n", params={})
        JackilTicket.objects.create(alert=alert, ticket_id=1)
        before = {m.__name__: m.objects.count()
                  for m in (Host, Alert, AlertRule, TaskRun, Task, JackilTicket)}

        client = _authed(user=_admin("boss"))
        for url in (PING, f"{SEARCH}?q=web1", f"{RESOLVE}?type=run&ids={run.id}",
                    _detail("host", host.id)):
            self.assertEqual(client.get(url).status_code, 200, url)
        self.assertEqual({m.__name__: m.objects.count()
                          for m in (Host, Alert, AlertRule, TaskRun, Task, JackilTicket)},
                         before)


# ── cross-site ────────────────────────────────────────────────────────────────
# Without the Business sites app there are no per-site roles, so nothing to be
# scoped out of; the module skips rather than passing vacuously.

require_business_module()
from apps_business.sites.models import (
    AutomationSiteAssignment,
    HostSiteAssignment,
    PlaybookSiteAssignment,
    Site,
    UserSiteRole,
)

#: The kinds whose visibility *is* site membership. ``task`` is absent on
#: purpose: a task definition is not a row on a host and has never been
#: site-scoped — the library's rule is "yours plus community ones", asserted
#: separately in `test_task_visibility_is_the_librarys_rule_not_a_site`.
#:
#: What this file exists to catch is an integration endpoint that forgets a
#: filter, and the worst version of it: search hiding a Lab row that detail
#: then hands over. So all three are checked against the same rows.
_SITE_MEMBERSHIP_TYPES = ("host", "playbook", "automation", "run")


class IntegrationLeakTests(TestCase):
    def setUp(self):
        self.west = Site.objects.create(name="West Campus", slug="west-campus")
        self.lab = Site.objects.create(name="Lab", slug="lab")

        self.dana = get_user_model().objects.create_user("dana", password="x")
        UserSiteRole.objects.create(user=self.dana, site=self.west, role=Role.ADMIN)
        self.boss = get_user_model().objects.create_user("boss", password="x")
        UserProfile.objects.create(user=self.boss, role=Role.ADMIN)

        definition = TaskDefinition.objects.create(
            name="d", risk_level="standard", yaml_source="",
            parsed_spec={"risk": "standard", "actions": []})

        # One of everything in each site, named so a leak is unmistakable.
        for site, tag in ((self.west, "west"), (self.lab, "lab")):
            host = Host.objects.create(hostname=f"{tag}-host", agent_token=f"tok-{tag}",
                                       os=f"{tag}os", status=Host.Status.ONLINE,
                                       tags=[tag])
            HostSiteAssignment.objects.create(host=host, site=site)
            setattr(self, f"{tag}_host", host)

            rule = AlertRule.objects.create(name=f"{tag}-rule", category="cpu",
                                            metric="cpu", operator="gt", threshold=90,
                                            severity="warning")
            alert = Alert.objects.create(host=host, rule=rule, severity="warning",
                                         message=f"{tag} alert")
            run = TaskRun.objects.create(name_snapshot=f"{tag} run", host_count=1)
            Task.objects.create(host=host, run=run, action="check_service",
                                nonce=f"n-{tag}", params={})
            JackilTicket.objects.create(alert=alert, ticket_id=1)

            playbook = Playbook.objects.create(name=f"{tag}-playbook",
                                               description=f"the {tag} one")
            PlaybookSiteAssignment.objects.create(playbook=playbook, site=site)

            automation = Automation.objects.create(
                name=f"{tag}-automation", trigger=Automation.Trigger.SCHEDULE,
                action_kind=Automation.ActionKind.TASK, task_definition=definition,
                target=Automation.Target.ALL)
            AutomationSiteAssignment.objects.create(automation=automation, site=site)

        self.dana_key = _authed(user=self.dana)
        self.boss_key = _authed(user=self.boss)

    def _labels(self, client, q, kind=None):
        params = {"q": q} if kind is None else {"q": q, "type": kind}
        return [row["label"] for row in client.get(SEARCH, params).json()["results"]]

    def _lab_ids(self, kind):
        """The Lab rows' ids, taken through the unscoped key's own eyes."""
        return [row["id"] for row in self.boss_key.get(SEARCH, {"q": "lab"}).json()["results"]
                if row["type"] == kind]

    def _ids(self, client, q, kind):
        return [row["id"] for row in
                client.get(SEARCH, {"q": q, "type": kind}).json()["results"]]

    def test_the_fixture_puts_a_row_of_every_kind_in_each_site(self):
        """The leak assertions below take their ids from search, so an empty
        result would otherwise read as "nothing leaked" rather than as the
        fixture having nothing to leak."""
        for kind in _SITE_MEMBERSHIP_TYPES:
            with self.subTest(kind=kind):
                self.assertEqual(len(self._lab_ids(kind)), 1,
                                 f"the unscoped key should find exactly one Lab {kind}")
                self.assertEqual(len(self._ids(self.boss_key, "west", kind)), 1,
                                 f"the unscoped key should find exactly one West {kind}")

    def test_search_hides_another_sites_rows_for_every_kind(self):
        """The search page must not name a Lab row to a West-only key."""
        for kind in _SITE_MEMBERSHIP_TYPES:
            with self.subTest(kind=kind):
                labels = self._labels(self.dana_key, "lab", kind)
                self.assertEqual(labels, [], f"{kind} leaked Lab rows: {labels}")

    def test_search_still_finds_the_callers_own_site(self):
        for kind, q in (("host", "west"), ("playbook", "west"), ("automation", "west"),
                        ("run", "west")):
            with self.subTest(kind=kind):
                labels = self._labels(self.dana_key, q, kind)
                self.assertTrue(any("west" in x.lower() for x in labels),
                                f"{kind} hid the caller's own site: {labels}")

    def test_resolve_omits_another_sites_ids(self):
        """Asking with Lab's ids must not buy Lab's rows — and must not say
        anything either, so a probe cannot tell them from an id that never
        existed."""
        for kind in TYPES:
            with self.subTest(kind=kind):
                ids = self._lab_ids(kind)
                if kind == "task":
                    # Definitions are the library's own visibility rule, not a
                    # site membership: this asserts that, not a leak.
                    continue
                self.assertTrue(ids, f"no Lab {kind} was found by the unscoped key")
                body = self.dana_key.get(RESOLVE, {"type": kind,
                                                   "ids": ",".join(ids)}).json()
                self.assertEqual(body, {"items": {}}, f"{kind} resolved out-of-site rows")

    def test_detail_is_404_for_another_sites_row(self):
        for kind in ("host", "playbook", "automation", "run"):
            with self.subTest(kind=kind):
                pk = self._lab_ids(kind)[0]
                response = self.dana_key.get(_detail(kind, pk))
                self.assertEqual(response.status_code, 404, response.content)
                self.assertEqual(response.json(), {"error": "Not found"})

    def test_detail_of_another_sites_row_is_unknowable(self):
        """The 404 a scoped key gets is byte-identical to the one for an id
        that never existed, so probing ids cannot discover a Lab row."""
        pk = self._lab_ids("host")[0]
        hidden = self.dana_key.get(_detail("host", pk))
        unknown = self.dana_key.get(_detail("host", UNKNOWN_HOST))
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden.content, unknown.content)

    def test_the_finders_are_the_pages_own_filters(self):
        """A guard against a finder drifting from the page it copies: search,
        resolve and detail share one queryset per kind, so the leak tests are
        only as good as that queryset matching what the page itself shows."""
        from apps.hosts.search import _hosts as page_hosts

        # The hosts page and the key's finder must answer with the same rows,
        # in the same order, for the same query.
        request = type("R", (), {"user": self.dana})()
        self.assertEqual([row["id"] for row in page_hosts(request, "host")],
                         [str(pk) for pk in
                          _FINDERS["host"][0](self.dana).values_list("pk", flat=True)])
        self.assertEqual([row["id"] for row in page_hosts(request, "west")],
                         [str(self.west_host.id)])

    def test_an_unscoped_admins_key_sees_both_sites(self):
        """The other half: filtering must not be over-eager, or the fleet-wide
        admin's own helpdesk goes blind."""
        for kind in _SITE_MEMBERSHIP_TYPES:
            with self.subTest(kind=kind):
                labels = self._labels(self.boss_key, "lab", kind)
                self.assertTrue(any("lab" in x.lower() for x in labels),
                                f"{kind}: the unscoped key could not find Lab: {labels}")

    def test_task_visibility_is_the_librarys_rule_not_a_site(self):
        """A private definition belongs to its owner alone, whoever holds a
        site; a community one is browsable by every key. That is the task
        library's own rule (``search._tasks``), unchanged here."""
        TaskDefinition.objects.create(
            owner=self.dana, name="west private task", visibility="private",
            yaml_source="", parsed_spec={"risk": "standard", "actions": []})
        TaskDefinition.objects.create(
            owner=self.boss, name="lab community task", visibility="community",
            yaml_source="", parsed_spec={"risk": "standard", "actions": []})

        dana = _authed(user=self.dana)
        self.assertEqual(self._labels(dana, "private", "task"), ["west private task"])
        self.assertEqual(self._labels(self.boss_key, "private", "task"), [])
        self.assertEqual(self._labels(dana, "community", "task"), ["lab community task"])
        self.assertEqual(self._labels(self.boss_key, "community", "task"),
                         ["lab community task"])
