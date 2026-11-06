"""The load harness (sprint M4 phase 01): the seed command, the script's
pure helpers, and a short real run against a live server."""
import argparse
import asyncio
import importlib.util
import io
import os
import random
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import LiveServerTestCase, TestCase

from apps.hosts.models import Host
from apps.metrics.models import MetricPoint

_path = Path(__file__).resolve().parents[3] / "scripts" / "loadtest.py"
_spec = importlib.util.spec_from_file_location("loadtest", _path)
loadtest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loadtest)


def _seed(test, n=3, prefix="load"):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, "tokens.txt")
    call_command("loadtest_seed", "--hosts", str(n), "--out", path, "--prefix", prefix, stdout=io.StringIO())
    return path


class SeedTests(TestCase):
    def test_seed_makes_approved_hosts_with_working_tokens(self):
        path = _seed(self)
        hosts = Host.objects.filter(hostname__startswith="load-")
        self.assertEqual(hosts.count(), 3)
        self.assertEqual({(h.status, h.mode) for h in hosts},
                         {(Host.Status.ONLINE, Host.Mode.MANAGED)})
        tokens = Path(path).read_text().split()
        self.assertEqual(len(tokens), 3)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertNotIn(tokens[0], {h.agent_token for h in hosts}, "tokens are stored hashed")
        resp = self.client.post("/api/v1/checkin", loadtest.payload("load-00001", random.Random(1)),
                                content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {tokens[0]}")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(MetricPoint.objects.filter(host__hostname="load-00001").count(), 19)

    def test_seed_refuses_a_bad_prefix_and_a_second_run(self):
        with self.assertRaises(CommandError):
            call_command("loadtest_seed", "--hosts", "1", "--out", "/dev/null", "--prefix", "Bad Prefix")
        with self.assertRaises(CommandError):
            call_command("loadtest_seed", "--hosts", "1", "--out", "/dev/null", "--prefix", "with-hyphen")
        _seed(self, 1)
        with self.assertRaisesMessage(CommandError, "--delete"):
            _seed(self, 1)

    def test_delete_removes_only_loadtest_hosts(self):
        _seed(self, 2, prefix="other")
        _seed(self, 3)
        # Real hosts that happen to share the prefix: no "loadtest" tag, so kept.
        real = Host.objects.create(hostname="load-00001x", agent_token="real-token-0123456789")
        untagged = Host.objects.create(hostname="load-real", agent_token="real-token-abcdefghij")
        call_command("loadtest_seed", "--delete", stdout=io.StringIO())
        left = set(Host.objects.values_list("hostname", flat=True))
        self.assertEqual(left, {real.hostname, untagged.hostname, "other-00001", "other-00002"})


class HarnessTests(unittest.TestCase):
    def test_payload_shape(self):
        body = loadtest.payload("load-00007", random.Random(3))
        self.assertEqual(body["hostname"], "load-00007")
        self.assertEqual(len(body["metrics"]), 19)
        for point in body["metrics"]:
            self.assertEqual(set(point), {"category", "metric", "value", "labels", "time"})
            self.assertTrue(point["time"].endswith("+00:00"))
        self.assertIn({"core": "total"}, [p["labels"] for p in body["metrics"]])
        again = loadtest.payload("load-00007", random.Random(3))
        self.assertEqual([p["value"] for p in body["metrics"]], [p["value"] for p in again["metrics"]],
                         "one seed, one run")

    def test_percentile(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(loadtest.percentile(values, 50), 5)
        self.assertEqual(loadtest.percentile(values, 95), 10)
        self.assertEqual(loadtest.percentile(values, 0), 1)
        self.assertEqual(loadtest.percentile([], 50), 0.0)

    def test_summarize(self):
        out = loadtest.summarize([10.0, 20.0, 30.0], {200: 3, 500: 1, 0: 2}, 63, 2.0, 4)
        self.assertEqual(out["checkins"], 3)
        self.assertEqual(out["errors"], {0: 2, 500: 1})
        self.assertEqual(out["rows_per_s"], 31.5)
        self.assertEqual(out["checkins_per_s"], 1.5)
        self.assertEqual(out["p50_ms"], 20.0)
        self.assertEqual(out["max_celery_queue"], 4)


class LiveTests(LiveServerTestCase):
    def test_a_short_run_against_a_live_server(self):
        path = _seed(self)
        args = argparse.Namespace(server=self.live_server_url, tokens=path, hosts=3, interval=1.0,
                                  duration=3.0, metrics_token=None, report=None, seed=1)
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            code = asyncio.run(loadtest.run(args))
        self.assertEqual(code, 0)
        for name in ("load-00001", "load-00002", "load-00003"):
            self.assertTrue(MetricPoint.objects.filter(host__hostname=name).exists(), name)

    def test_a_bad_token_is_counted_as_an_error(self):
        path = _seed(self, 1)
        Path(path).write_text("load-00001-" + "0" * 32 + "\n")
        args = argparse.Namespace(server=self.live_server_url, tokens=path, hosts=0, interval=1.0,
                                  duration=1.5, metrics_token=None, report=None, seed=1)
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(asyncio.run(loadtest.run(args)), 1)

