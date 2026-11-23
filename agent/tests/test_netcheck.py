"""run_check: tcp, http, dns, icmp (recorded ping output only) and the never-raises contract.

Everything here is local. No ping is run and nothing leaves the machine.
"""

import http.server
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from vigil_agent import netcheck
from vigil_agent.netcheck import _days_left, _cert_problem, _tls_reason, run_check

NOW = datetime(2027, 5, 29, 12, 0, 0, tzinfo=timezone.utc)

LINUX_OK = """PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.
64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.045 ms
64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=0.051 ms
64 bytes from 10.0.0.1: icmp_seq=3 ttl=64 time=0.058 ms

--- 10.0.0.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2030ms
rtt min/avg/max/mdev = 0.045/0.051/0.058/0.005 ms
"""

LINUX_DOWN = """PING 10.0.0.9 (10.0.0.9) 56(84) bytes of data.

--- 10.0.0.9 ping statistics ---
3 packets transmitted, 0 received, +3 errors, 100% packet loss, time 2054ms
"""

MACOS_OK = """PING 10.0.0.1 (10.0.0.1): 56 data bytes
64 bytes from 10.0.0.1: icmp_seq=0 ttl=64 time=0.412 ms
64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.601 ms
64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=0.481 ms

--- 10.0.0.1 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 0.412/0.498/0.601/0.081 ms
"""

MACOS_DOWN = """PING 10.0.0.9 (10.0.0.9): 56 data bytes

--- 10.0.0.9 ping statistics ---
3 packets transmitted, 0 packets received, 100.0% packet loss
"""

FREEBSD_OK = """PING 10.0.0.1 (10.0.0.1): 56 data bytes
64 bytes from 10.0.0.1: icmp_seq=0 ttl=64 time=0.210 ms
64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.262 ms
64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=0.236 ms

--- 10.0.0.1 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 0.210/0.236/0.262/0.022 ms
"""

FREEBSD_DOWN = """PING 10.0.0.9 (10.0.0.9): 56 data bytes

--- 10.0.0.9 ping statistics ---
3 packets transmitted, 0 packets received, 100.0% packet loss
"""

WINDOWS_OK = """
Pinging 10.0.0.1 with 32 bytes of data:
Reply from 10.0.0.1: bytes=32 time=1ms TTL=64
Reply from 10.0.0.1: bytes=32 time<1ms TTL=64
Reply from 10.0.0.1: bytes=32 time=2ms TTL=64

Ping statistics for 10.0.0.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 2ms, Average = 1ms
"""

WINDOWS_DOWN = """
Pinging 10.0.0.9 with 32 bytes of data:
Request timed out.
Request timed out.
Request timed out.

Ping statistics for 10.0.0.9:
    Packets: Sent = 3, Received = 0, Lost = 3 (100% loss),
"""


def subprocess_result(stdout, returncode=0):
    return mock.Mock(stdout=stdout, stderr="", returncode=returncode)


class TcpTests(unittest.TestCase):
    def test_open_port_is_ok_with_latency(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        out = run_check({"kind": "tcp", "host": "127.0.0.1", "port": port})

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["kind"], "tcp")
        self.assertEqual(out["target"], "127.0.0.1")
        self.assertEqual(out["port"], port)
        self.assertIsNotNone(out["latency_ms"])
        self.assertGreaterEqual(out["latency_ms"], 0.0)
        self.assertEqual(out["detail"], "port is open")

    def test_closed_port_is_refused(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        out = run_check({"kind": "tcp", "host": "127.0.0.1", "port": port})

        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "connection refused")
        self.assertIsNone(out["latency_ms"])

    def test_invalid_port_is_rejected_without_connecting(self):
        for port in (0, 65536, "22", True, None):
            with self.subTest(port=port):
                with mock.patch("vigil_agent.netcheck.socket.create_connection") as conn:
                    out = run_check({"kind": "tcp", "host": "127.0.0.1", "port": port})
                conn.assert_not_called()
                self.assertFalse(out["ok"])
                self.assertIn("port", out["detail"])


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {"/": 200, "/down": 503, "/moved": 302}

    def do_GET(self):  # noqa: N802 — http.server naming
        code = self.routes.get(self.path, 404)
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep test output quiet
        pass


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_200_is_ok(self):
        out = run_check({"kind": "http", "url": self.base + "/"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["detail"], "HTTP 200")
        self.assertIsNotNone(out["latency_ms"])
        self.assertIsNone(out["cert_days_left"])
        self.assertIsNone(out["cert_expires"])

    def test_503_is_not_ok_by_default(self):
        out = run_check({"kind": "http", "url": self.base + "/down"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], 503)
        self.assertEqual(out["detail"], "HTTP 503")

    def test_expect_status_overrides_the_default_range(self):
        out = run_check({"kind": "http", "url": self.base + "/down", "expect_status": [503]})
        self.assertTrue(out["ok"], out)

        out = run_check({"kind": "http", "url": self.base + "/", "expect_status": [503]})
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "HTTP 200, expected 503")

    def test_redirect_is_not_followed(self):
        out = run_check({"kind": "http", "url": self.base + "/moved"})
        self.assertEqual(out["status"], 302)
        self.assertTrue(out["ok"])

    def test_unknown_path_is_404(self):
        out = run_check({"kind": "http", "url": self.base + "/nothing-here"})
        self.assertEqual(out["status"], 404)
        self.assertFalse(out["ok"])

    def test_non_http_schemes_are_refused_without_a_request(self):
        for url in ("file:///etc/passwd", "ftp://x", "gopher://x"):
            with self.subTest(url=url):
                with mock.patch("vigil_agent.netcheck.requests.get") as get:
                    out = run_check({"kind": "http", "url": url})
                get.assert_not_called()
                self.assertFalse(out["ok"])
                self.assertEqual(out["detail"], "only http and https URLs are checked")

    def test_connection_refused_is_reported(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        out = run_check({"kind": "http", "url": f"http://127.0.0.1:{port}/"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "connection refused")

    def test_bad_expect_status_is_rejected_before_the_request(self):
        with mock.patch("vigil_agent.netcheck.requests.get") as get:
            out = run_check({"kind": "http", "url": self.base + "/", "expect_status": "200"})
        get.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "invalid expect_status")


class CertHelperTests(unittest.TestCase):
    def test_days_left_parses_getpeercert_format(self):
        self.assertEqual(_days_left("Jun  1 12:00:00 2027 GMT", NOW), 3)

    def test_days_left_is_negative_once_expired(self):
        self.assertEqual(_days_left("May 27 12:00:00 2027 GMT", NOW), -2)

    def test_days_left_handles_a_space_padded_single_digit_day(self):
        self.assertEqual(_days_left("Jun  2 12:00:00 2027 GMT", NOW), 4)

    def test_threshold_below_minimum_is_a_problem(self):
        self.assertEqual(_cert_problem(3, 14), "certificate expires in 3 days")

    def test_threshold_one_day_is_singular(self):
        self.assertEqual(_cert_problem(1, 14), "certificate expires in 1 day")

    def test_threshold_at_or_above_minimum_is_fine(self):
        self.assertIsNone(_cert_problem(14, 14))
        self.assertIsNone(_cert_problem(90, 14))

    def test_expired_certificate_is_a_problem(self):
        self.assertEqual(_cert_problem(-2, 14), "certificate has expired")

    def test_tls_reason_maps_common_verify_messages(self):
        cases = {
            "certificate verify failed: self-signed certificate": "self-signed certificate",
            "certificate verify failed: certificate has expired": "certificate has expired",
            "hostname 'x' doesn't match 'y'": "hostname does not match certificate",
            "unable to get local issuer certificate": "issuer not trusted",
            "something else entirely": "handshake failed",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(_tls_reason(Exception(message)), expected)


class HttpsTests(unittest.TestCase):
    """Real TLS against a self-signed certificate made by the openssl command."""

    @classmethod
    def setUpClass(cls):
        # The openssl command makes the certificate: the agent has no
        # cryptography package, and adding one for a test is not worth it.
        import shutil
        import subprocess as sp

        if not shutil.which("openssl"):
            raise unittest.SkipTest("no openssl command to make a self-signed certificate")
        cls._tmp = tempfile.TemporaryDirectory()
        cert_path = Path(cls._tmp.name) / "cert.pem"
        key_path = Path(cls._tmp.name) / "key.pem"
        sp.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-nodes", "-days", "3", "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1",
                "-keyout", str(key_path), "-out", str(cert_path)],
               check=True, capture_output=True, timeout=30)
        cls.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.ctx.load_cert_chain(cert_path, key_path)

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.socket = cls.ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"https://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
        if hasattr(cls, "_tmp"):
            cls._tmp.cleanup()

    def test_untrusted_certificate_fails_with_tls_detail(self):
        out = run_check({"kind": "http", "url": self.url, "verify_tls": True})
        self.assertFalse(out["ok"])
        self.assertTrue(out["detail"].startswith("TLS:"), out["detail"])

    def test_unverified_request_is_ok_and_skips_expiry(self):
        out = run_check({"kind": "http", "url": self.url, "verify_tls": False})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], 200)
        self.assertIsNone(out["cert_days_left"])
        self.assertIsNone(out["cert_expires"])


class IcmpIpv6Tests(unittest.TestCase):
    def test_ipv6_uses_ping6_on_macos_and_dash_6_on_windows(self):
        from vigil_agent import netcheck
        for plat, argv in (("darwin", ["ping6", "-c", "3"]),
                           ("win32", ["ping", "-6", "-n", "3", "-w", "5000"]),
                           ("linux", ["ping", "-c", "3", "-W", "5"])):
            with self.subTest(plat), mock.patch.object(sys, "platform", plat):
                self.assertEqual(netcheck._ping_argv("2001:db8::1", 3, 5.0), argv)


class IcmpTests(unittest.TestCase):
    def _run(self, platform, stdout, host="10.0.0.1", count=None, timeout=None, returncode=0):
        check = {"kind": "icmp", "host": host}
        if count is not None:
            check["count"] = count
        kwargs = {} if timeout is None else {"timeout": timeout}
        with mock.patch.object(sys, "platform", platform), mock.patch(
            "vigil_agent.netcheck.subprocess.run",
            return_value=subprocess_result(stdout, returncode),
        ) as run:
            out = run_check(check, **kwargs)
        return out, run

    def test_linux_success_argv_and_parse(self):
        out, run = self._run("linux", LINUX_OK)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["ping", "-c", "3", "-W", "5", "10.0.0.1"])
        self.assertEqual(run.call_args.kwargs["timeout"], 17.0)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["kind"], "icmp")
        self.assertEqual(out["target"], "10.0.0.1")
        self.assertEqual(out["sent"], 3)
        self.assertEqual(out["received"], 3)
        self.assertEqual(out["loss_percent"], 0.0)
        self.assertEqual(out["latency_ms"], 0.1)

    def test_linux_total_loss(self):
        out, _ = self._run("linux", LINUX_DOWN, host="10.0.0.9")
        self.assertFalse(out["ok"])
        self.assertEqual(out["sent"], 3)
        self.assertEqual(out["received"], 0)
        self.assertEqual(out["loss_percent"], 100.0)
        self.assertIsNone(out["latency_ms"])
        self.assertEqual(out["detail"], "no replies received")

    def test_macos_uses_milliseconds_for_wait(self):
        out, run = self._run("darwin", MACOS_OK)
        self.assertEqual(run.call_args.args[0], ["ping", "-c", "3", "-W", "5000", "10.0.0.1"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["latency_ms"], 0.5)
        self.assertEqual(out["received"], 3)

    def test_macos_total_loss(self):
        out, _ = self._run("darwin", MACOS_DOWN, host="10.0.0.9")
        self.assertFalse(out["ok"])
        self.assertEqual(out["loss_percent"], 100.0)
        self.assertIsNone(out["latency_ms"])

    def test_freebsd_uses_overall_timeout_flag(self):
        out, run = self._run("freebsd14", FREEBSD_OK)
        self.assertEqual(run.call_args.args[0], ["ping", "-c", "3", "-t", "15", "10.0.0.1"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["latency_ms"], 0.2)

    def test_freebsd_total_loss(self):
        out, _ = self._run("freebsd14", FREEBSD_DOWN, host="10.0.0.9")
        self.assertFalse(out["ok"])
        self.assertEqual(out["loss_percent"], 100.0)

    def test_windows_uses_count_and_milliseconds_flags(self):
        out, run = self._run("win32", WINDOWS_OK)
        self.assertEqual(run.call_args.args[0], ["ping", "-n", "3", "-w", "5000", "10.0.0.1"])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["sent"], 3)
        self.assertEqual(out["received"], 3)
        self.assertEqual(out["loss_percent"], 0.0)
        self.assertEqual(out["latency_ms"], 1.0)

    def test_windows_total_loss(self):
        out, _ = self._run("win32", WINDOWS_DOWN, host="10.0.0.9")
        self.assertFalse(out["ok"])
        self.assertEqual(out["loss_percent"], 100.0)
        self.assertEqual(out["received"], 0)
        self.assertIsNone(out["latency_ms"])

    def test_count_is_clamped(self):
        _, run = self._run("linux", LINUX_OK, count=50)
        self.assertEqual(run.call_args.args[0][2], "10")
        _, run = self._run("linux", LINUX_OK, count=0)
        self.assertEqual(run.call_args.args[0][2], "1")

    def test_unknown_platform_does_not_run_ping(self):
        with mock.patch.object(sys, "platform", "plan9"), mock.patch(
            "vigil_agent.netcheck.subprocess.run"
        ) as run:
            out = run_check({"kind": "icmp", "host": "10.0.0.1"})
        run.assert_not_called()
        self.assertFalse(out["ok"])

    def test_unparseable_output_is_a_failure(self):
        out, _ = self._run("linux", "ping: unknown host\n", returncode=2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "unexpected ping output")

    def test_invalid_hosts_never_reach_subprocess(self):
        bad = ["-oProxy", "a;b", "$(id)", "host name", "", "x" * 254, "a|b", "-"]
        for host in bad:
            with self.subTest(host=host):
                with mock.patch("vigil_agent.netcheck.subprocess.run") as run:
                    out = run_check({"kind": "icmp", "host": host})
                run.assert_not_called()
                self.assertFalse(out["ok"])
                self.assertEqual(out["detail"], "invalid host" if host else "missing host")

    def test_ipv6_literal_is_accepted(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch(
            "vigil_agent.netcheck.subprocess.run", return_value=subprocess_result(LINUX_OK)
        ) as run:
            out = run_check({"kind": "icmp", "host": "fe80::1"})
        self.assertEqual(run.call_args.args[0][-1], "fe80::1")
        self.assertTrue(out["ok"])


class DnsTests(unittest.TestCase):
    def test_localhost_resolves(self):
        out = run_check({"kind": "dns", "name": "localhost"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(
            "127.0.0.1" in out["addresses"] or "::1" in out["addresses"], out["addresses"]
        )
        self.assertEqual(out["addresses"], sorted(set(out["addresses"])))

    def test_expect_present_is_ok(self):
        out = run_check({"kind": "dns", "name": "localhost", "expect": ["127.0.0.1"]})
        if "127.0.0.1" not in out["addresses"]:
            self.skipTest("localhost does not resolve to 127.0.0.1 on this host")
        self.assertTrue(out["ok"])

    def test_expect_missing_is_not_ok(self):
        with mock.patch(
            "vigil_agent.netcheck.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 1, 6, "", ("93.184.216.34", 0))],
        ):
            out = run_check({"kind": "dns", "name": "example.test", "expect": ["10.9.9.9"]})
        self.assertFalse(out["ok"])
        self.assertEqual(out["addresses"], ["93.184.216.34"])
        self.assertEqual(out["detail"], "resolved, but not to 10.9.9.9")

    def test_resolved_addresses_are_sorted_and_unique(self):
        infos = [
            (socket.AF_INET, 1, 6, "", ("10.0.0.2", 0)),
            (socket.AF_INET, 1, 6, "", ("10.0.0.1", 0)),
            (socket.AF_INET, 1, 6, "", ("10.0.0.2", 0)),
        ]
        with mock.patch("vigil_agent.netcheck.socket.getaddrinfo", return_value=infos):
            out = run_check({"kind": "dns", "name": "two.test"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["addresses"], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(out["detail"], "resolved to 2 addresses")

    def test_unresolvable_name_is_not_ok(self):
        out = run_check({"kind": "dns", "name": "nonexistent.invalid"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["addresses"], [])
        self.assertIsNone(out["latency_ms"])

    def test_slow_lookup_times_out(self):
        def slow(*args, **kwargs):
            time.sleep(1.0)
            return []

        started = time.monotonic()
        with mock.patch("vigil_agent.netcheck.socket.getaddrinfo", side_effect=slow):
            out = run_check({"kind": "dns", "name": "slow.test"}, timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["detail"])
        self.assertLess(elapsed, 0.9)


class ContractTests(unittest.TestCase):
    JUNK = [
        None,
        5,
        "icmp",
        [],
        {},
        {"kind": None},
        {"kind": 5},
        {"kind": ["icmp"]},
        {"kind": "nope"},
        {"kind": "icmp"},
        {"kind": "icmp", "host": None},
        {"kind": "icmp", "host": 7},
        {"kind": "icmp", "host": "10.0.0.1", "count": "3"},
        {"kind": "tcp"},
        {"kind": "tcp", "host": "127.0.0.1", "port": "22"},
        {"kind": "tcp", "host": None, "port": 22},
        {"kind": "http"},
        {"kind": "http", "url": None},
        {"kind": "http", "url": 3},
        {"kind": "http", "url": "http://[bad"},
        {"kind": "http", "url": "http://127.0.0.1:1/", "expect_status": "200"},
        {"kind": "http", "url": "http://127.0.0.1:1/", "verify_tls": "yes"},
        {"kind": "http", "url": "http://127.0.0.1:1/", "expect_cert_days": -1},
        {"kind": "dns"},
        {"kind": "dns", "name": None},
        {"kind": "dns", "name": "x.invalid", "expect": "1.2.3.4"},
    ]

    def test_junk_inputs_return_ok_false_results(self):
        for check in self.JUNK:
            with self.subTest(check=check):
                with mock.patch("vigil_agent.netcheck.subprocess.run") as run, mock.patch(
                    "vigil_agent.netcheck.requests.get"
                ) as get:
                    out = run_check(check)
                run.assert_not_called()
                get.assert_not_called()
                self.assertIsInstance(out, dict)
                self.assertIs(out["ok"], False)
                self.assertIsInstance(out["kind"], str)
                self.assertIsInstance(out["target"], str)
                self.assertIsInstance(out["detail"], str)
                self.assertIsNone(out["latency_ms"])

    def test_bad_timeout_values_fall_back_to_default(self):
        for timeout in (None, "soon", -1, float("nan")):
            with self.subTest(timeout=timeout):
                out = run_check({"kind": "tcp", "host": "127.0.0.1", "port": 1}, timeout=timeout)
                self.assertIs(out["ok"], False)

    def test_timeout_is_clamped_to_the_maximum(self):
        self.assertEqual(netcheck._clamp_timeout(999), netcheck.MAX_TIMEOUT)
        self.assertEqual(netcheck._clamp_timeout(2), 2.0)


if __name__ == "__main__":
    unittest.main()
