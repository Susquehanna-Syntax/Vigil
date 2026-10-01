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
