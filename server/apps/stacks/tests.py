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
        bar = (root / "templates/pages/_containers.html").read_text(encoding="utf-8")
        page = (root / "templates/pages/_stack_editor.html").read_text(encoding="utf-8")
        base = (root / "templates/base.html").read_text(encoding="utf-8")
        self.assertIn('id="managed-stacks"', bar)
        self.assertIn("js/vigil-stacks.js", base)
        for marker in ('id="page-stack-editor"', 'id="stack-compose"', 'data-stack-save',
                       'data-stack-deploy', 'data-stack-remove', 'data-stack-env-reveal'):
            self.assertIn(marker, page)
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


class SandboxEscapeTests(SimpleTestCase):
    """validate_compose is what keeps a stack from owning its host where
    stack_deploy is allowlisted but scripts are not (architect review,
    2026-10-08). Each of these reached the host another way."""

    S = "services:\n  a:\n    image: x\n"

    def test_more_escapes_are_refused(self):
        bad = {
            "cap sys_admin": self.S + "    cap_add: [SYS_ADMIN]\n",
            "cap sys_module lowercase": self.S + "    cap_add: [cap_sys_module]\n",
            "cap sys_ptrace": self.S + "    cap_add: [SYS_PTRACE]\n",
            "raw disk device": self.S + "    devices: ['/dev/sda:/dev/sda']\n",
            "device long form": self.S + "    devices:\n      - /dev/mem\n",
            "apparmor unconfined": self.S + "    security_opt: ['apparmor:unconfined']\n",
            "seccomp unconfined": self.S + "    security_opt: ['seccomp=unconfined']\n",
            "label disable": self.S + "    security_opt: ['label:disable']\n",
            "ipc host": self.S + "    ipc: host\n",
            "userns host": self.S + "    userns_mode: host\n",
            "cgroup host": self.S + "    cgroup: host\n",
            "home bind": self.S + "    volumes: ['~/.ssh:/keys']\n",
            "env_file absolute": self.S + "    env_file: /etc/shadow\n",
            "env_file climb": self.S + "    env_file: ['../../etc/x']\n",
            "build context absolute": "services:\n  a:\n    build: /\n",
            "build context climb": "services:\n  a:\n    build: {context: ../..}\n",
            "named volume binds etc": self.S + "    volumes: ['data:/d']\nvolumes:\n  data:\n    driver_opts: {type: none, o: bind, device: /etc}\n",
            "secret from host file": self.S + "secrets:\n  s:\n    file: /etc/shadow\n",
            "config from host file": self.S + "configs:\n  c:\n    file: /root/.ssh/id_rsa\n",
        }
        for label, text in bad.items():
            with self.subTest(label):
                with self.assertRaises(StackError):
                    validate_compose(text)

    def test_ordinary_setups_still_pass(self):
        good = [
            self.S + "    devices: ['/dev/dri:/dev/dri']\n",
            self.S + "    devices: ['/dev/net/tun:/dev/net/tun']\n    cap_add: [NET_ADMIN]\n",
            self.S + "    network_mode: host\n",
            self.S + "    env_file: .env\n",
            "services:\n  a:\n    build: ./app\n",
            self.S + "    volumes: ['data:/d']\nvolumes:\n  data: {}\n",
            self.S + "secrets:\n  s:\n    file: ./secret.txt\n",
        ]
        for text in good:
            with self.subTest(text=text):
                validate_compose(text)


class SandboxAllowlistTests(SimpleTestCase):
    """The denylist missed routes compose offers; the sandbox now allows only
    keys it knows, refuses $ variables where a value decides what the
    container can reach, and reads YAML booleans the way compose does."""

    S = "services:\n  a:\n    image: x\n"

    def test_more_routes_are_refused(self):
        bad = {
            "volumes_from another container": self.S + "    volumes_from: ['container:portainer']\n",
            "include other compose files": "include: ['/opt/other/compose.yaml']\n" + self.S,
            "extends a file": "services:\n  a:\n    extends: {file: /opt/x.yaml, service: b}\n",
            "device cgroup rules": self.S + "    device_cgroup_rules: ['b 8:* rmw']\n",
            "api socket": self.S + "    use_api_socket: true\n",
            "privileged as string": self.S + "    privileged: 'true'\n",
            "privileged as yes": self.S + "    privileged: yes\n",
            "variable bind source": self.S + "    volumes: ['${HOSTDIR}:/data']\n",
            "variable long bind": self.S + "    volumes:\n      - {type: bind, source: '${D}', target: /d}\n",
            "variable cap": self.S + "    cap_add: ['${CAP}']\n",
            "variable device": self.S + "    devices: ['${DEV}:/dev/x']\n",
            "variable privileged": self.S + "    privileged: ${P}\n",
            "variable env_file": self.S + "    env_file: ${F}\n",
            "variable driver device": self.S + "    volumes: ['d:/d']\nvolumes:\n  d:\n    driver_opts: {o: bind, device: '${X}'}\n",
            "unknown service key": self.S + "    some_future_key: true\n",
            "unknown top-level key": "something: else\n" + self.S,
            "pid of another container": self.S + "    pid: 'container:other'\n",
            "host sysctl": self.S + "    sysctls: {kernel.core_pattern: '|/tmp/x'}\n",
        }
        for label, text in bad.items():
            with self.subTest(label):
                with self.assertRaises(StackError):
                    validate_compose(text)

    def test_ordinary_variables_and_keys_still_pass(self):
        good = [
            self.S + "    environment: {TZ: '${TZ:-UTC}', DB: '${DB_PASSWORD}'}\n",
            "services:\n  a:\n    image: 'nginx:${TAG:-stable}'\n    ports: ['${PORT:-80}:80']\n",
            self.S + "    volumes_from: [b]\n  b:\n    image: y\n",
            self.S + "    sysctls: {net.core.somaxconn: 1024}\n",
            self.S + "    deploy:\n      resources:\n        limits: {memory: 512M}\n",
            "x-common: &c {restart: always}\n" + self.S + "    <<: *c\n",
            self.S + "    healthcheck: {test: ['CMD', 'true']}\n    labels: {a: b}\n    logging: {driver: json-file}\n",
        ]
        for text in good:
            with self.subTest(text=text):
                validate_compose(text)


class SandboxReviewTests(SimpleTestCase):
    """Second review of the allowlist: hooks, build options and device paths."""

    S = "services:\n  a:\n    image: x\n"

    def test_refused(self):
        bad = {
            "privileged post_start hook": self.S + "    post_start: [{command: id, privileged: true}]\n",
            "pre_stop hook": self.S + "    pre_stop: [{command: id}]\n",
            "develop watch": self.S + "    develop: {watch: [{path: /etc, action: sync, target: /x}]}\n",
            "runtime": self.S + "    runtime: runc-unsafe\n",
            "build additional context": "services:\n  a:\n    build: {context: ., additional_contexts: {h: /etc}}\n",
            "build ssh": "services:\n  a:\n    build: {context: ., ssh: [default]}\n",
            "build entitlements": "services:\n  a:\n    build: {context: ., entitlements: [network.host]}\n",
            "build network host": "services:\n  a:\n    build: {context: ., network: host}\n",
            "build cache_from local": "services:\n  a:\n    build: {context: ., cache_from: ['type=local,src=/etc']}\n",
            "build cache_to local": "services:\n  a:\n    build: {context: ., cache_to: ['type=local,dest=/etc']}\n",
            "build privileged": "services:\n  a:\n    build: {context: ., privileged: true}\n",
            "device climbs out": self.S + "    devices: ['/dev/dri/../sda:/dev/sda']\n",
            "device prefix trick": self.S + "    devices: ['/dev/fuse-not-really:/x']\n",
            "nvidia prefix trick": self.S + "    devices: ['/dev/nvidiaXYZ:/x']\n",
        }
        for label, text in bad.items():
            with self.subTest(label):
                with self.assertRaises(StackError):
                    validate_compose(text)

    def test_still_allowed(self):
        good = [
            "services:\n  a:\n    build: {context: ./app, dockerfile: Dockerfile, args: {V: 1}, target: prod}\n",
            "services:\n  a:\n    build: {context: ., additional_contexts: {base: 'docker-image://alpine:3', b: 'service:b'}}\n  b:\n    image: y\n",
            "services:\n  a:\n    build: {context: ., cache_from: ['type=registry,ref=ghcr.io/x/y:cache']}\n",
            self.S + "    devices: ['/dev/dri/renderD128:/dev/dri/renderD128', '/dev/nvidia0', '/dev/nvidiactl', '/dev/net/tun', '/dev/fuse']\n",
        ]
        for text in good:
            with self.subTest(text=text):
                validate_compose(text)


class SandboxAncestorTests(SimpleTestCase):
    """A bind of a directory that *contains* a forbidden path reaches it too."""

    S = "services:\n  a:\n    image: x\n"

    def test_refused(self):
        bad = {
            "/run holds the engine socket": self.S + "    volumes: ['/run:/host-run']\n",
            "/var/run (a link to /run)": self.S + "    volumes: ['/var/run:/r']\n",
            "/var holds /var/lib/docker": self.S + "    volumes: ['/var:/v']\n",
            "/var/lib": self.S + "    volumes: ['/var/lib:/l']\n",
            "containerd socket dir": self.S + "    volumes: ['/run/containerd:/c']\n",
            "agent data": self.S + "    volumes: ['/var/lib/vigil-agent:/a']\n",
            "variable in a key": self.S + "    volumes: ['d:/d']\nvolumes:\n  d:\n    driver_opts: {'${K}': /etc}\n",
            "annotations": self.S + "    annotations: {run.oci.keep_original_groups: '1'}\n",
            "double leading slash": self.S + "    volumes: ['//run:/r']\n",
            "triple slash etc": self.S + "    volumes: ['///etc/:/e']\n",
            "pid Host": self.S + "    pid: Host\n",
            "ipc HOST": self.S + "    ipc: ' HOST '\n",
        }
        for label, text in bad.items():
            with self.subTest(label):
                with self.assertRaises(StackError):
                    validate_compose(text)

    def test_data_paths_still_allowed(self):
        for path in ("/srv/media", "/mnt/storage", "/home/alice/music", "/var/log/app", "/opt/app-data"):
            with self.subTest(path):
                validate_compose(self.S + f"    volumes: ['{path}:/data']\n")


class SandboxPodmanTests(SimpleTestCase):
    """x-* is an inert extension field to docker, but podman-compose acts on
    x-podman (podman_args, pod_args, uidmaps): refused at any depth."""

    def test_x_podman_is_refused(self):
        S = "services:\n  a:\n    image: x\n"
        for text in (S + "x-podman:\n  in_pod: false\n",
                     S + "    x-podman:\n      podman_args: [--privileged]\n",
                     S + "    X-Podman.uidmaps: ['0:0:1']\n"):
            with self.subTest(text=text), self.assertRaises(StackError):
                validate_compose(text)
        validate_compose("x-common: &c {restart: always}\n" + S)


class SandboxDosTests(SimpleTestCase):
    def test_an_anchor_bomb_is_checked_quickly(self):
        import time
        lines = ["x-a0: &a0 [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]"]
        for i in range(1, 25):
            lines.append(f"x-a{i}: &a{i} [" + ", ".join([f"*a{i-1}"] * 10) + "]")
        text = "\n".join(lines) + "\nservices:\n  a:\n    image: x\n"
        start = time.monotonic()
        validate_compose(text)
        self.assertLess(time.monotonic() - start, 2.0)
