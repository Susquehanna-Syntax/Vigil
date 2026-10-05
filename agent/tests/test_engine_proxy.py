"""M11 01b: the monitor-mode engine proxy forwards reads and nothing else."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from tests.fake_engine import FakeEngine
from vigil_agent import engine, engine_proxy


class AllowListTests(unittest.TestCase):
    def test_reads_only(self):
        ok = ["/_ping", "/v1.43/version", "/v1.43/containers/json?all=1",
              "/v1.43/containers/abc123/json", "/v1.43/containers/web-1/stats?stream=false",
              "/v1.43/containers/abc/logs?tail=100&stdout=1", "/v1.43/images/json"]
        for path in ok:
            self.assertTrue(engine_proxy.allowed("GET", path), path)
        refused = [("POST", "/v1.43/containers/json"), ("POST", "/v1.43/containers/abc/restart"), ("DELETE", "/v1.43/containers/abc"),
                   ("POST", "/v1.43/containers/abc/exec"), ("GET", "/v1.43/containers/abc/stats"),
                   ("GET", "/v1.43/containers/abc/logs?follow=true"), ("GET", "/v1.43/volumes"),
                   ("GET", "/v1.43/containers/../../etc/json"), ("GET", "/v1.43/containers/abc/archive")]
        for method, path in refused:
            self.assertFalse(engine_proxy.allowed(method, path), (method, path))


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEngine(routes={("GET", "/containers/json"): (200, [{"Id": "abc"}]),
                                       ("POST", "/containers/abc/stop"): (204, b"")})
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, "ro.sock")
        self.server = engine_proxy.make_server(engine.EngineClient(self.fake.path), path=path)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = engine.EngineClient(path)
        self.path = path

    def test_reads_pass_and_writes_never_reach_the_engine(self):
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")
        self.assertEqual(self.client.get("/containers/json"), [{"Id": "abc"}])
        with self.assertRaises(engine.EngineError) as ctx:
            self.client.post("/containers/abc/stop")
        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(any(m == "POST" for m, _p, _b in self.fake.requests))


if __name__ == "__main__":
    unittest.main()
