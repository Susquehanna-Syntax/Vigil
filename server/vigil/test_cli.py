"""The `vigil` command line (cli/), run against this server (sprint M5).

Two nets:

* **The contract.** Every endpoint and response field the CLI names in
  ``vigil_cli.main.ENDPOINTS`` must be in the OpenAPI schema, so renaming a
  route or a field fails here rather than on someone's machine.
* **The real thing.** A live server, the real sign-in forms, real TOTP codes
  and the CLI's own code end to end: nothing between them is patched.
"""
import io
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import LiveServerTestCase, SimpleTestCase
from django.utils.timezone import now
from drf_spectacular.generators import SchemaGenerator

from apps.accounts.models import Role, UserProfile
from apps.accounts.totp import generate_secret, generate_totp
from apps.agent_dist.models import AgentBinary
from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.tasks.models import Task, TaskDefinition, TaskRun
from apps.tasks.spec import parse_and_validate

sys.path.insert(0, str(Path(settings.BASE_DIR).parent / "cli"))

from vigil_cli import main as cli  # noqa: E402
from vigil_cli import store  # noqa: E402
from vigil_cli.client import Client, CliError  # noqa: E402

YAML = "name: Update agent\nrisk: standard\nactions:\n  - type: update_agent\n"
PASSWORD = "correct horse battery staple"


class CliContractTests(SimpleTestCase):
    """What the CLI reads is what the schema promises."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def _fields(self, schema: dict) -> set:
        if "$ref" in schema:
            schema = self.schema["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
        if schema.get("type") == "array":
            return self._fields(schema["items"])
        return set(schema.get("properties", {}))

    def test_every_endpoint_and_field_is_in_the_schema(self):
        for name, (method, path, fields) in cli.ENDPOINTS.items():
            with self.subTest(name):
                operation = self.schema["paths"].get(path, {}).get(method.lower())
                self.assertIsNotNone(operation, f"{method} {path} is not in the schema")
                ok = next(v for k, v in operation["responses"].items() if k.startswith("2"))
                promised = self._fields(ok["content"]["application/json"]["schema"])
                self.assertEqual(set(fields) - promised, set(),
                                 f"{method} {path} does not promise these")

    def test_the_query_parameters_the_cli_sends_are_documented(self):
        for path, wanted in (("/api/v1/alerts/", {"state"}),
                             ("/api/v1/tasks/definitions/", {"scope"})):
            with self.subTest(path):
                documented = {p["name"] for p in self.schema["paths"][path]["get"]
                              .get("parameters", []) if p["in"] == "query"}
                self.assertEqual(wanted - documented, set())


class CliLiveTests(LiveServerTestCase):
    def setUp(self):
        # A live-server test empties the database between tests, so the row a
        # migration seeded is gone: put the Global site back where sites exist.
        from django.apps import apps
        if apps.is_installed("apps_business.sites"):
            apps.get_model("business_sites", "Site").objects.get_or_create(
                is_global=True, defaults={"name": "Global", "slug": "global"})
        self.user = get_user_model().objects.create_user("op", password=PASSWORD)
        self.secret = generate_secret()
        UserProfile.objects.create(user=self.user, role=Role.OPERATOR,
                                   totp_secret=self.secret, totp_confirmed_at=now())
        self.host = Host.objects.create(
            hostname="web-01", agent_token="z" * 32, os="Debian 13",
            status=Host.Status.ONLINE, mode=Host.Mode.MANAGED, tags=["prod", "web"])
        Host.objects.create(hostname="db-01", agent_token="y" * 32,
                            status=Host.Status.OFFLINE, mode=Host.Mode.MONITOR)
        AgentBinary.objects.create(platform="linux-amd64", version="1.0", sha256="a" * 64)
        self.definition = TaskDefinition.objects.create(
            owner=self.user, name="Update agent", yaml_source=YAML,
            parsed_spec=parse_and_validate(YAML))
        rule = AlertRule.objects.create(name="CPU high", category="cpu", metric="usage_percent",
                                        threshold=90)
        self.alert = Alert.objects.create(host=self.host, rule=rule, state=Alert.State.FIRING,
                                          message="CPU at 97%", metric_value=97)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "vigil" / "session.json"
        env = mock.patch.dict(os.environ, {"VIGIL_CONFIG": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("VIGIL_API_KEY", "VIGIL_URL"):
            os.environ.pop(name, None)
        self._codes = 0

    # ── helpers ──────────────────────────────────────────────────────────────

    def code(self) -> str:
        """A fresh code each time: the server refuses one it has seen. Its
        window allows one 30-second step either side of now, so a test has
        three."""
        self._codes += 1
        return generate_totp(self.secret, at=time.time() + (0, 30, -30)[self._codes - 1])

    def run_cli(self, *argv, answers=(), password=PASSWORD):
        out, err = io.StringIO(), io.StringIO()
        answers = list(answers)
        status = cli.main(list(argv), out=out, err=err,
                          prompt=lambda _q: answers.pop(0), secret=lambda _q: password,
                          sleep=lambda _s: None)
        return status, out.getvalue(), err.getvalue()

    def login(self):
        status, out, err = self.run_cli("login", "--url", self.live_server_url, "--user", "op",
                                        answers=[self.code()])
        self.assertEqual(status, 0, err)
        return out

    # ── signing in ───────────────────────────────────────────────────────────

    def test_login_stores_a_session_only_its_owner_can_read(self):
        out = self.login()
        self.assertIn(f"Signed in to {self.live_server_url} as op", out)
        mode = stat.S_IMODE(self.config.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)
        saved = json.loads(self.config.read_text())
        self.assertEqual(set(saved["cookies"]), {"sessionid", "csrftoken"})
        self.assertNotIn(PASSWORD, self.config.read_text())

    def test_a_wrong_password_is_refused_in_the_forms_own_words(self):
        status, _out, err = self.run_cli("login", "--url", self.live_server_url, "--user", "op",
                                         password="nope")
        self.assertEqual(status, 2)
        self.assertIn("Invalid username or password", err)
        self.assertFalse(self.config.exists())

    def test_a_wrong_code_is_refused_and_nothing_is_stored(self):
        status, _out, err = self.run_cli("login", "--url", self.live_server_url, "--user", "op",
                                         answers=["000000"])
        self.assertEqual(status, 2)
        self.assertTrue(err.strip())
        self.assertFalse(self.config.exists())

    def test_an_account_without_a_second_factor_is_sent_to_the_browser(self):
        get_user_model().objects.create_user("new", password=PASSWORD)
        status, _out, err = self.run_cli("login", "--url", self.live_server_url, "--user", "new")
        self.assertEqual(status, 2)
        self.assertIn("no second factor yet", err)

    def test_the_password_alone_reaches_nothing(self):
        client = Client(self.live_server_url)
        client.begin_login("op", PASSWORD)  # parked as pending, not signed in
        with self.assertRaises(CliError):
            client.api("GET", "/api/v1/hosts/")

    def test_commands_without_a_sign_in_say_so(self):
        status, _out, err = self.run_cli("hosts", "--url", self.live_server_url)
        self.assertEqual(status, 2)
        self.assertIn("vigil login", err)

    def test_logout_ends_the_session_on_the_server_too(self):
        self.login()
        cookies = json.loads(self.config.read_text())["cookies"]
        self.assertEqual(self.run_cli("logout")[0], 0)
        self.assertFalse(self.config.exists())
        with self.assertRaises(CliError):
            Client(self.live_server_url, cookies=cookies).api("GET", "/api/v1/hosts/")

    def test_a_session_the_server_ended_asks_for_a_new_login(self):
        self.login()
        from django.contrib.sessions.models import Session
        Session.objects.all().delete()
        status, _out, err = self.run_cli("alerts")
        self.assertEqual(status, 3)
        self.assertIn("vigil login", err)
        self.assertNotIn("cookies", json.loads(self.config.read_text()))

    def test_the_saved_session_is_never_sent_to_another_server(self):
        self.login()
        status, _out, err = self.run_cli("hosts", "--url", "https://elsewhere.example")
        self.assertEqual(status, 2)
        self.assertIn("Not signed in", err)

    def test_plain_http_to_another_machine_is_refused_unless_allowed(self):
        with self.assertRaises(CliError):
            Client("http://vigil.example.com")
        Client("http://vigil.example.com", allow_http=True)
        Client("http://localhost:8000")

    # ── reading ──────────────────────────────────────────────────────────────

    def test_hosts_lists_filters_and_shows_one(self):
        self.login()
        _s, out, _e = self.run_cli("hosts")
        self.assertIn("web-01", out)
        self.assertIn("db-01", out)
        self.assertLess(out.index("db-01"), out.index("web-01"), "sorted by name")
        _s, out, _e = self.run_cli("hosts", "--status", "online", "--tag", "PROD")
        self.assertIn("web-01", out)
        self.assertNotIn("db-01", out)
        _s, out, _e = self.run_cli("hosts", "WEB-01")
        self.assertIn(str(self.host.id), out)
        self.assertIn("Debian 13", out)
        status, _out, err = self.run_cli("hosts", "nope")
        self.assertEqual(status, 2)
        self.assertIn("No host named", err)

    def test_json_is_the_servers_own_answer_wherever_the_flag_sits(self):
        self.login()
        for argv in (("hosts", "--json"), ("--json", "hosts")):
            with self.subTest(argv):
                _s, out, _e = self.run_cli(*argv)
                self.assertEqual({h["hostname"] for h in json.loads(out)}, {"web-01", "db-01"})

    def test_alerts_list_and_acknowledge_by_the_start_of_an_id(self):
        self.login()
        _s, out, _e = self.run_cli("alerts")
        self.assertIn("CPU at 97%", out)
        self.assertIn(str(self.alert.id)[:8], out)
        status, out, err = self.run_cli("alerts", "ack", str(self.alert.id)[:8], "--for", "2h")
        self.assertEqual(status, 0, err)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, Alert.State.ACKNOWLEDGED)
        seconds = (self.alert.acknowledged_until - now()).total_seconds()
        self.assertAlmostEqual(seconds, 7200, delta=60)
        self.assertIn("No firing alerts.", self.run_cli("alerts")[1])
        self.assertIn("CPU at 97%", self.run_cli("alerts", "--state", "acknowledged")[1])

    def test_one_bad_id_acknowledges_nothing(self):
        self.login()
        status, _out, err = self.run_cli("alerts", "ack", str(self.alert.id), "ffffffff")
        self.assertEqual(status, 2)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.state, Alert.State.FIRING)

    def test_tasks_and_the_api_escape_hatch(self):
        self.login()
        _s, out, _e = self.run_cli("tasks")
        self.assertIn("Update agent", out)
        _s, out, _e = self.run_cli("api", "GET", "/api/v1/hosts/tags/")
        json.loads(out)
        status, _out, err = self.run_cli("api", "GET", "/api/v1/hosts")
        self.assertEqual(status, 1)
        self.assertIn("end with a slash", err)

    # ── deploying ────────────────────────────────────────────────────────────

    def test_deploy_asks_for_a_code_and_starts_a_run(self):
        self.login()
        status, out, err = self.run_cli("deploy", "update agent", "--host", "web-01",
                                        answers=[self.code()])
        self.assertEqual(status, 0, err)
        run = TaskRun.objects.get()
        self.assertIn(str(run.id)[:8], out)
        self.assertEqual(list(Task.objects.values_list("host__hostname", flat=True)), ["web-01"])
        self.assertIn("'Update agent'", err, "what is about to run is said before the code is asked")

    def test_deploy_with_a_wrong_code_runs_nothing(self):
        self.login()
        status, _out, err = self.run_cli("deploy", "Update agent", "--host", "web-01",
                                         answers=["000000"])
        self.assertEqual(status, 1)
        self.assertTrue(err.strip())
        self.assertFalse(TaskRun.objects.exists())
        # Refused, not signed out: the next command still works.
        self.assertEqual(self.run_cli("hosts")[0], 0)

    def test_deploy_refuses_an_unknown_host_before_asking_for_a_code(self):
        self.login()
        status, _out, err = self.run_cli("deploy", "Update agent", "--host", "web-01",
                                         "--host", "typo")  # no answers: a prompt would raise
        self.assertEqual(status, 2)
        self.assertIn("No host named 'typo'", err)
        self.assertFalse(TaskRun.objects.exists())

    def test_wait_follows_the_run_and_its_exit_code_is_the_outcome(self):
        self.login()

        def finish(state, task_state):
            def _sleep(_seconds):
                Task.objects.update(state=task_state, result_output="done\nmore")
                TaskRun.objects.update(state=state)
            return _sleep

        for run_state, task_state, expected in (
                (TaskRun.State.COMPLETED, Task.State.COMPLETED, 0),
                (TaskRun.State.FAILED, Task.State.FAILED, 1)):
            with self.subTest(run_state):
                out, err = io.StringIO(), io.StringIO()
                answers = [self.code()]
                status = cli.main(["deploy", "Update agent", "--host", "web-01", "--wait"],
                                  out=out, err=err, prompt=lambda _q: answers.pop(0),
                                  secret=lambda _q: PASSWORD,
                                  sleep=finish(run_state, task_state))
                self.assertEqual(status, expected, err.getvalue())
                self.assertIn(run_state, out.getvalue())
                self.assertIn("done ...", out.getvalue())

    def test_runs_lists_and_shows_one(self):
        self.login()
        self.run_cli("deploy", "Update agent", "--host", "web-01", answers=[self.code()])
        run = TaskRun.objects.get()
        _s, out, _e = self.run_cli("runs")
        self.assertIn(str(run.id)[:8], out)
        _s, out, _e = self.run_cli("runs", str(run.id)[:8])
        self.assertIn("web-01", out)
        self.assertIn("pending", out)


class CliDocsTests(SimpleTestCase):
    """The wiki's command table names commands and options that exist."""

    def test_every_documented_command_parses(self):
        import re
        import shlex

        wiki = (Path(settings.BASE_DIR).parent / "wiki" / "vigil-wiki.html").read_text()
        start = wiki.index('<h3 id="cli">')
        section = wiki[start:wiki.index("<h3>Hosts</h3>", start)]
        commands = [c for c in re.findall(r"<code>(vigil [^<]+)</code>", section)]
        self.assertGreaterEqual(len(commands), 10)
        parser = cli.build_parser()
        for command in commands:
            with self.subTest(command):
                argv = shlex.split(command.replace("&quot;", '"'))[1:]
                try:
                    args = parser.parse_args(argv)
                except SystemExit:
                    self.fail(f"the wiki documents a command the CLI refuses: {command}")
                self.assertTrue(callable(getattr(args, "func", None)))
        def helps(p):
            yield p.format_help()
            for action in p._actions:
                for sub in (getattr(action, "choices", None) or {}).values() \
                        if hasattr(action, "_name_parser_map") else ():
                    yield from helps(sub)

        every_help = "".join(helps(parser))
        for option in set(re.findall(r"<code>(--[a-z-]+)", section)):
            with self.subTest(option):
                self.assertIn(option, every_help)
