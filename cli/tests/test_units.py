"""The parts of the CLI that need no Vigil: parsing, picking, tables, the
session file, and what goes on the wire for each kind of credential. The
end-to-end tests against a real server are server/vigil/test_cli.py."""
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from vigil_cli import main as cli
from vigil_cli import store
from vigil_cli.client import ApiError, CliError, Client, SignedOut, normalise_url
from vigil_cli.output import ago, table


class Recorder(BaseHTTPRequestHandler):
    """Answers every request from ``routes`` and remembers what it was sent."""

    routes: dict = {}
    seen: list = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        Recorder.seen.append({"method": self.command, "path": self.path,
                              "headers": dict(self.headers),
                              "body": self.rfile.read(length).decode()})
        status, body = Recorder.routes.get((self.command, self.path.split("?")[0]),
                                           (404, {"detail": "Not found."}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", "tracking=1; Path=/")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Recorder.seen = []
        Recorder.routes = {("GET", "/api/v1/hosts/"): (200, [{"id": "a" * 32, "hostname": "h1",
                                                             "status": "online", "mode": "managed"}])}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "session.json"
        env = mock.patch.dict(os.environ, {"VIGIL_CONFIG": str(self.config),
                                           "VIGIL_URL": self.url}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("VIGIL_API_KEY", None)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        status = cli.main(list(argv), out=out, err=err, prompt=lambda _q: "123456",
                          secret=lambda _q: "pw", sleep=lambda _s: None)
        return status, out.getvalue(), err.getvalue()

    def test_an_api_key_is_sent_as_a_bearer_token_and_nothing_is_stored(self):
        with mock.patch.dict(os.environ, {"VIGIL_API_KEY": "vsk_secret"}):
            status, out, err = self.run_cli("hosts")
        self.assertEqual(status, 0, err)
        self.assertIn("h1", out)
        sent = Recorder.seen[-1]["headers"]
        self.assertEqual(sent["Authorization"], "Bearer vsk_secret")
        self.assertNotIn("Cookie", sent)
        self.assertFalse(self.config.exists())

    def test_a_session_sends_its_cookies_and_the_csrf_token_on_writes(self):
        Recorder.routes[("POST", "/api/v1/x/")] = (200, {})
        client = Client(self.url, cookies={"sessionid": "s1", "csrftoken": "c1", "other": "x"})
        client.api("GET", "/api/v1/hosts/")
        get = Recorder.seen[-1]["headers"]
        self.assertEqual(get["Cookie"], "sessionid=s1; csrftoken=c1")
        self.assertNotIn("X-Csrftoken", {k.title() for k in get})
        client.api("POST", "/api/v1/x/", {"a": 1})
        post = {k.lower(): v for k, v in Recorder.seen[-1]["headers"].items()}
        self.assertEqual(post["x-csrftoken"], "c1")
        self.assertEqual(post["referer"], self.url + "/")
        self.assertEqual(json.loads(Recorder.seen[-1]["body"]), {"a": 1})

    def test_only_the_session_cookies_are_kept(self):
        client = Client(self.url, cookies={"sessionid": "s1"})
        client.api("GET", "/api/v1/hosts/")
        self.assertEqual(client.cookies, {"sessionid": "s1"})  # not "tracking"

    def test_error_bodies_become_one_sentence(self):
        for body, expected in (({"error": "Nope"}, "Nope"), ({"detail": "Gone"}, "Gone"),
                               ({"errors": {"name": "is taken"}}, "name: is taken"),
                               ({}, "The server answered 400.")):
            Recorder.routes[("GET", "/api/v1/bad/")] = (400, body)
            with self.subTest(body), self.assertRaises(ApiError) as caught:
                Client(self.url, cookies={"sessionid": "s"}).api("GET", "/api/v1/bad/")
            self.assertEqual(str(caught.exception), expected)

    def test_an_ended_session_is_told_apart_from_a_refused_request(self):
        client = Client(self.url, cookies={"sessionid": "s"})
        Recorder.routes[("GET", "/api/v1/a/")] = (401, {"detail": "Signed out after a period "
                                                                  "of inactivity.",
                                                        "reason": "session_idle"})
        with self.assertRaises(SignedOut):
            client.api("GET", "/api/v1/a/")
        Recorder.routes[("GET", "/api/v1/a/")] = (
            403, {"detail": "Authentication credentials were not provided."})
        with self.assertRaises(SignedOut):
            client.api("GET", "/api/v1/a/")
        Recorder.routes[("GET", "/api/v1/a/")] = (401, {"error": "Invalid TOTP code"})
        with self.assertRaises(ApiError) as caught:
            client.api("GET", "/api/v1/a/")
        self.assertNotIsInstance(caught.exception, SignedOut)

    def test_an_unreachable_server_is_said_plainly(self):
        with self.assertRaises(CliError) as caught:
            Client("http://127.0.0.1:9").api("GET", "/api/v1/hosts/")
        self.assertIn("Could not reach", str(caught.exception))


class AddressTests(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(normalise_url("vigil.example.com/"), "https://vigil.example.com")
        self.assertEqual(normalise_url("https://v.example.com:8443/hosts/"), "https://v.example.com:8443")
        self.assertEqual(normalise_url("http://localhost:8000"), "http://localhost:8000")
        for bad in ("", "ftp://x", "https://user:pw@v.example.com", "http://vigil.lan"):
            with self.subTest(bad), self.assertRaises(CliError):
                normalise_url(bad)
        self.assertEqual(normalise_url("http://vigil.lan", allow_http=True), "http://vigil.lan")

    def test_a_path_must_be_a_path(self):
        with self.assertRaises(CliError):
            Client("http://localhost:1").api("GET", "api/v1/hosts/")


class PickingTests(unittest.TestCase):
    ROWS = [{"id": "3f2b7c91-0000", "name": "web"}, {"id": "3f2bffff-0000", "name": "db"},
            {"id": "9a4e1c77-0000", "name": "Web"}]

    def test_pick(self):
        self.assertEqual(cli.pick(self.ROWS, "DB", name_key="name", what="host")["id"], "3f2bffff-0000")
        self.assertEqual(cli.pick(self.ROWS, "9a4e", name_key="name", what="host")["name"], "Web")
        for wanted, said in (("web", "2 hosts are named"), ("3f2b", "start of 2 host ids"),
                             ("3f", "No host named"), ("zzzz", "No host named")):
            with self.subTest(wanted), self.assertRaises(CliError) as caught:
                cli.pick(self.ROWS, wanted, name_key="name", what="host")
            self.assertIn(said, str(caught.exception))

    def test_durations(self):
        self.assertEqual([cli.parse_duration(t) for t in ("30", "45s", "30m", "2h", "1d", " 2 H ")],
                         [1800, 45, 1800, 7200, 86400, 7200])
        for bad in ("", "0", "soon", "-5m", "1w"):
            with self.subTest(bad), self.assertRaises(CliError):
                cli.parse_duration(bad)


class OutputTests(unittest.TestCase):
    def test_table(self):
        self.assertEqual(table(["A", "LONGER"], [["x", 1], ["yyyy", None]]),
                         "A     LONGER\nx     1\nyyyy  -")
        self.assertEqual(table(["A"], [], empty="None."), "None.")
        self.assertEqual(table(["A"], [["two\nlines  here"]]), "A\ntwo lines here")

    def test_ago(self):
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        for delta, expected in ((timedelta(seconds=5), "5s ago"), (timedelta(minutes=3), "3m ago"),
                                (timedelta(hours=5), "5h ago"), (timedelta(days=2), "2d ago"),
                                (timedelta(seconds=-30), "just now")):
            self.assertEqual(ago((now - delta).isoformat(), now=now), expected)
        self.assertEqual(ago("2026-11-16T11:59:00Z", now=now), "1m ago")
        self.assertEqual(ago(None), "never")
        self.assertEqual(ago("garbage"), "garbage")


class StoreTests(unittest.TestCase):
    def test_the_file_is_private_even_when_an_older_one_was_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "session.json"
            with mock.patch.dict(os.environ, {"VIGIL_CONFIG": str(target)}):
                store.save({"url": "https://v"})
                os.chmod(target, 0o644)
                store.save({"url": "https://v", "cookies": {"sessionid": "s"}})
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual(store.load()["cookies"], {"sessionid": "s"})
                self.assertEqual(os.listdir(target.parent), ["session.json"])
                store.clear()
                store.clear()
                self.assertEqual(store.load(), {})
                target.write_text("not json")
                self.assertEqual(store.load(), {})


if __name__ == "__main__":
    unittest.main()
