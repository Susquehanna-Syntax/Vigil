"""M11 08: managed stacks — validated compose, encrypted .env, revisions, masking."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from .models import ManagedStack, StackRevision
from .validation import StackError, parse_env, validate_compose

COMPOSE = """services:
  jellyfin:
    image: jellyfin/jellyfin:10.9
    volumes:
      - /srv/media:/media:ro
      - ./config:/config
      - cache:/cache
    env_file: .env
volumes:
  cache: {}
"""
_TOTP = "apps.accounts.totp.require_totp_confirmation"


class ValidationTests(SimpleTestCase):
    def test_a_normal_stack_passes(self):
        validate_compose(COMPOSE)

    def test_refusals(self):
        bad = {
            "not yaml": "services: [",
            "no services": "volumes: {}\n",
            "privileged": "services:\n  a:\n    image: x\n    privileged: true\n",
            "pid host": "services:\n  a:\n    image: x\n    pid: host\n",
            "cap all": "services:\n  a:\n    image: x\n    cap_add: [ALL]\n",
            "docker socket": "services:\n  a:\n    image: x\n    volumes: ['/var/run/docker.sock:/var/run/docker.sock']\n",
            "etc": "services:\n  a:\n    image: x\n    volumes: ['/etc:/host-etc:ro']\n",
            "root": "services:\n  a:\n    image: x\n    volumes: ['/:/host']\n",
            "climb": "services:\n  a:\n    image: x\n    volumes: ['../../etc:/x']\n",
            "long form": "services:\n  a:\n    image: x\n    volumes:\n      - {type: bind, source: /root, target: /r}\n",
        }
        for label, text in bad.items():
            with self.subTest(label):
                with self.assertRaises(StackError):
                    validate_compose(text)

    def test_env_parsing(self):
        self.assertEqual(parse_env("# c\nA=1\n\nB=x=y\n"), [("A", "1"), ("B", "x=y")])
        for bad in ("NOEQUALS\n", "1BAD=x\n", "A B=1\n"):
            with self.subTest(bad=bad), self.assertRaises(StackError):
                parse_env(bad)


class StackApiTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="nas", agent_token="nastok", status=Host.Status.ONLINE,
                                        mode="managed")
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def _create(self, **extra):
        body = {"host_id": str(self.host.id), "name": "media", "compose_yaml": COMPOSE,
                "env_text": "TZ=Europe/London\nAPI_KEY=hunter2\n", **extra}
        return self.client_api.post("/api/v1/stacks/", body, format="json")

    def test_create_masks_and_encrypts(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["env"], [{"key": "TZ", "set": True}, {"key": "API_KEY", "set": True}])
        self.assertNotIn("hunter2", resp.content.decode())
        stack = ManagedStack.objects.get()
        self.assertNotIn(b"hunter2", bytes(stack.env_encrypted))
        self.assertEqual(stack.working_dir, "/opt/vigil/stacks/media")
        self.assertEqual(StackRevision.objects.get().number, 1)

    def test_edit_keeps_unchanged_secrets_and_makes_a_revision(self):
        sid = self._create().json()["id"]
        resp = self.client_api.put(f"/api/v1/stacks/{sid}/", {"env": [
            {"key": "TZ", "value": "UTC"}, {"key": "API_KEY", "keep": True}, {"key": "NEW", "value": "1"}],
            "note": "tz"}, format="json")
        self.assertEqual(resp.json()["revision"], 2)
        with mock.patch(_TOTP, return_value=None):
            env = self.client_api.post(f"/api/v1/stacks/{sid}/env/reveal/", {"totp": "1"},
                                       format="json").json()["env"]
        self.assertEqual(env, [{"key": "TZ", "value": "UTC"}, {"key": "API_KEY", "value": "hunter2"},
                               {"key": "NEW", "value": "1"}])
        revs = self.client_api.get(f"/api/v1/stacks/{sid}/revisions/").json()["results"]
        self.assertEqual([(r["number"], r["note"]) for r in revs], [(2, "tz"), (1, "created")])
        self.assertNotIn("hunter2", str(revs))

    def test_refusals(self):
        self.assertEqual(self._create(name="Bad Name").status_code, 400)
        self.assertEqual(self._create(compose_yaml="services: {a: {image: x, privileged: true}}").status_code, 400)
        self.assertEqual(self._create().status_code, 201)
        self.assertEqual(self._create().status_code, 400, "the same name twice on one host")
        sid = ManagedStack.objects.get().id
        resp = self.client_api.put(f"/api/v1/stacks/{sid}/", {"env": [{"key": "MISSING", "keep": True}]},
                                   format="json")
        self.assertEqual(resp.status_code, 400)
        with mock.patch(_TOTP, return_value="bad"):
            self.assertEqual(self.client_api.post(f"/api/v1/stacks/{sid}/env/reveal/", {}, format="json").status_code, 401)
        viewer = get_user_model().objects.create_user("v", password="x")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        c = APIClient()
        c.force_authenticate(viewer)
        self.assertEqual(c.get("/api/v1/stacks/").status_code, 403)


class StackDeployTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(hostname="nas", agent_token="nastok", status=Host.Status.ONLINE,
                                        mode="managed")
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.sid = self.api.post("/api/v1/stacks/", {
            "host_id": str(self.host.id), "name": "media", "compose_yaml": COMPOSE,
            "env_text": "API_KEY=hunter2\n"}, format="json").json()["id"]

    def _deploy(self):
        with mock.patch(_TOTP, return_value=None):
            return self.api.post(f"/api/v1/stacks/{self.sid}/deploy/", {"totp": "1"}, format="json")

    def _redeem(self, ticket, token="nastok"):
        return self.client.get(f"/api/v1/agent/stack-env/{ticket}/", HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_deploy_sends_a_ticket_not_the_secret(self):
        from apps.tasks.models import Task
        resp = self._deploy()
        self.assertEqual(resp.status_code, 201, resp.content)
        task = Task.objects.get(pk=resp.json()["task"])
        self.assertEqual(task.risk_level, "high")
        params = task.params["steps"][0]["params"]
        self.assertEqual((params["project"], params["working_dir"], params["revision"]),
                         ("media", "/opt/vigil/stacks/media", 1))
        self.assertNotIn("hunter2", str(task.params))
        ticket = params["env_ticket"]
        self.assertEqual(self._redeem(ticket).json(), {"env": "API_KEY=hunter2\n"})
        self.assertEqual(self._redeem(ticket).status_code, 404, "one time only")

    def test_ticket_is_for_its_host_and_expires(self):
        from datetime import timedelta

        from django.utils.timezone import now

        from .models import EnvTicket
        Host.objects.create(hostname="other", agent_token="othertok", status=Host.Status.ONLINE)
        self._deploy()
        ticket = EnvTicket.objects.get()
        self.assertEqual(self._redeem(ticket.id, "othertok").status_code, 404)
        EnvTicket.objects.filter(pk=ticket.pk).update(expires_at=now() - timedelta(seconds=1))
        self.assertEqual(self._redeem(ticket.id).status_code, 404)

    def test_deploy_ships_the_revision_it_was_issued_for(self):
        self._deploy()
        from .models import EnvTicket
        ticket = EnvTicket.objects.get()
        self.api.put(f"/api/v1/stacks/{self.sid}/", {"env": [{"key": "API_KEY", "value": "new"}]},
                     format="json")
        self.assertEqual(self._redeem(ticket.id).json()["env"], "API_KEY=hunter2\n")

    def test_remove_and_totp(self):
        from apps.tasks.models import Task
        with mock.patch(_TOTP, return_value="bad"):
            self.assertEqual(self.api.post(f"/api/v1/stacks/{self.sid}/deploy/", {}, format="json").status_code, 401)
        self.assertFalse(Task.objects.exists())
        with mock.patch(_TOTP, return_value=None):
            resp = self.api.post(f"/api/v1/stacks/{self.sid}/remove/", {"totp": "1", "delete_files": True},
                                 format="json")
        params = Task.objects.get(pk=resp.json()["task"]).params["steps"][0]["params"]
        self.assertEqual(params, {"project": "media", "working_dir": "/opt/vigil/stacks/media",
                                  "delete_files": True})


class StackSpecTests(SimpleTestCase):
    def _spec(self, **params):
        import yaml

        from apps.tasks.spec import parse_and_validate
        base = {"project": "media", "working_dir": "/opt/vigil/stacks/media", "compose": COMPOSE}
        return parse_and_validate(yaml.safe_dump({"name": "t", "actions": [
            {"type": "stack_deploy", "params": {**base, **params}}]}))

    def test_task_spec_applies_the_same_checks(self):
        from apps.tasks.spec import SpecError
        self.assertEqual(self._spec()["risk"], "high")
        for bad in ({"compose": "services: {a: {image: x, privileged: true}}"},
                    {"working_dir": "/opt/../etc"}, {"working_dir": "relative"},
                    {"project": "Media"}, {"env_ticket": "API_KEY=hunter2"}):
            with self.subTest(bad=bad), self.assertRaises(SpecError):
                self._spec(**bad)


class StackAuditTests(TestCase):
    def test_saving_revealing_and_deploying_are_audited_without_values(self):
        from apps_business.audits.apps import wire
        from apps_business.audits.models import AuditEvent
        wire()
        host = Host.objects.create(hostname="nas", agent_token="nastok", status=Host.Status.ONLINE,
                                   mode="managed")
        user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        api = APIClient()
        api.force_authenticate(user)
        sid = api.post("/api/v1/stacks/", {"host_id": str(host.id), "name": "media", "compose_yaml": COMPOSE,
                                           "env_text": "API_KEY=hunter2\n"}, format="json").json()["id"]
        with mock.patch(_TOTP, return_value=None):
            api.post(f"/api/v1/stacks/{sid}/env/reveal/", {"totp": "1"}, format="json")
            api.post(f"/api/v1/stacks/{sid}/deploy/", {"totp": "1"}, format="json")
        events = list(AuditEvent.objects.order_by("id").values_list("action", "target"))
        self.assertEqual(sorted(e for e in events if e[0].startswith("stack.")),
                         [("stack.deployed", "media@nas"), ("stack.env_revealed", "media@nas"),
                          ("stack.saved", "media@nas")])
        self.assertNotIn("hunter2", str(list(AuditEvent.objects.values())))


class StackEditorWiringTests(SimpleTestCase):
    def test_editor_is_wired(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        js = (root / "static/js/vigil-stacks.js").read_text(encoding="utf-8")
        page = (root / "templates/pages/_containers.html").read_text(encoding="utf-8")
        base = (root / "templates/base.html").read_text(encoding="utf-8")
        self.assertIn('id="managed-stacks"', page)
        self.assertIn("js/vigil-stacks.js", base)
        for needle in ("/env/reveal/", "/deploy/", "/remove/", "/revisions/", "keep: true",
                       "type=\"${e.revealed ? 'text' : 'password'}\"", "async function renderManagedStacks"):
            self.assertIn(needle, js)


class StackAdoptTests(TestCase):
    def setUp(self):
        from apps.hosts.models import ContainerStack
        self.host = Host.objects.create(hostname="nas", agent_token="nastok", status=Host.Status.ONLINE,
                                        mode="managed")
        ContainerStack.objects.create(host=self.host, project="shop", ownership="external")
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def _adopt(self):
        with mock.patch(_TOTP, return_value=None):
            return self.api.post("/api/v1/stacks/adopt/", {"host_id": str(self.host.id), "project": "shop",
                                                           "totp": "1"}, format="json")

    def _hand_over(self, ticket, **extra):
        body = {"project": "shop", "compose": COMPOSE, "env": "DB_PASSWORD=hunter2\n",
                "compose_file": "docker-compose.yml", "working_dir": "/srv/shop",
                "hashes": {"match": ["jellyfin"], "recreate": []}, **extra}
        return self.client.post(f"/api/v1/agent/stack-adopt/{ticket}/", body,
                                content_type="application/json", HTTP_AUTHORIZATION="Bearer nastok")

    def test_adopt_reads_in_place(self):
        from apps.hosts.models import ContainerStack
        from apps.tasks.models import Task
        resp = self._adopt()
        self.assertEqual(resp.status_code, 201, resp.content)
        task = Task.objects.get(pk=resp.json()["task"])
        self.assertEqual(task.params["steps"][0]["action"], "stack_read")
        self.assertEqual(self._hand_over(resp.json()["ticket"]).status_code, 200)
        stack = ManagedStack.objects.get()
        self.assertEqual((stack.adopted, stack.working_dir, stack.compose_file),
                         (True, "/srv/shop", "docker-compose.yml"))
        self.assertEqual(stack.adopt_report, {"match": ["jellyfin"], "recreate": []})
        self.assertNotIn(b"hunter2", bytes(stack.env_encrypted))
        self.assertEqual(ContainerStack.objects.get().ownership, "adopted")
        self.assertEqual(self._hand_over(resp.json()["ticket"]).status_code, 404, "one time only")
        # A deploy of the adopted stack writes its own file name back.
        with mock.patch(_TOTP, return_value=None):
            task_id = self.api.post(f"/api/v1/stacks/{stack.id}/deploy/", {"totp": "1"}, format="json").json()["task"]
        params = Task.objects.get(pk=task_id).params["steps"][0]["params"]
        self.assertEqual((params["working_dir"], params["compose_file"]), ("/srv/shop", "docker-compose.yml"))

    def test_a_compose_file_vigil_will_not_run_is_refused(self):
        ticket = self._adopt().json()["ticket"]
        resp = self._hand_over(ticket, compose="services:\n  a:\n    image: x\n    privileged: true\n")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(ManagedStack.objects.exists())
        from .models import AdoptTicket
        self.assertIn("privileged", AdoptTicket.objects.get().error)

    def test_refusals(self):
        self._adopt()
        self.assertEqual(self._adopt().status_code, 201, "a second ticket while the first is pending is fine")
        with mock.patch(_TOTP, return_value="bad"):
            self.assertEqual(self.api.post("/api/v1/stacks/adopt/", {"host_id": str(self.host.id),
                                                                     "project": "shop"}, format="json").status_code, 401)
        with mock.patch(_TOTP, return_value=None):
            self.assertEqual(self.api.post("/api/v1/stacks/adopt/", {"host_id": str(self.host.id),
                                                                     "project": "Shop!"}, format="json").status_code, 400)


class RegistryCredentialTests(TestCase):
    def setUp(self):
        self.office = Host.objects.create(hostname="office", agent_token="offtok", status=Host.Status.ONLINE,
                                          mode="managed", tags=["builders"])
        self.other = Host.objects.create(hostname="other", agent_token="othtok", status=Host.Status.ONLINE,
                                         mode="managed")
        user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(user)

    def _auth(self, token, registry="ghcr.io"):
        return self.client.get(f"/api/v1/agent/registry-auth/?registry={registry}",
                               HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_password_is_write_only_and_scoped_to_tagged_hosts(self):
        from .models import RegistryCredential
        resp = self.api.post("/api/v1/stacks/registries/", {"registry": "GHCR.io", "username": "bot",
                                                            "password": "s3cret", "host_tags": ["Builders"]},
                             format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertNotIn("s3cret", resp.content.decode())
        self.assertNotIn(b"s3cret", bytes(RegistryCredential.objects.get().password_encrypted))
        self.assertNotIn("s3cret", self.api.get("/api/v1/stacks/registries/").content.decode())
        self.assertEqual(self._auth("offtok").json(),
                         {"username": "bot", "password": "s3cret", "serveraddress": "ghcr.io"})
        self.assertEqual(self._auth("othtok").status_code, 404, "not a builders host")
        self.assertEqual(self._auth("offtok", "docker.io").status_code, 404)

    def test_untagged_credential_covers_every_host_and_refusals(self):
        self.api.post("/api/v1/stacks/registries/", {"registry": "registry.local:5000", "username": "u",
                                                     "password": "p"}, format="json")
        self.assertEqual(self._auth("othtok", "registry.local:5000").status_code, 200)
        for body in ({"registry": "https://ghcr.io", "username": "u", "password": "p"},
                     {"registry": "ghcr.io", "username": "", "password": "p"},
                     {"registry": "ghcr.io", "username": "u", "password": ""}):
            with self.subTest(body=body):
                self.assertEqual(self.api.post("/api/v1/stacks/registries/", body, format="json").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/agent/registry-auth/?registry=ghcr.io").status_code, 401)
