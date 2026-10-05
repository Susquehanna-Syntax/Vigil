"""M11 07: container_logs and the live tail."""
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from tests.fake_engine import FakeEngine
from vigil_agent import client, engine, executor
from vigil_agent.actions import logs
from vigil_agent.config import AgentConfig, _ALL_ACTIONS

_CFG = AgentConfig(server_url="https://v", agent_token="t", mode="monitor")
SESSION = "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def frame(stream: int, text: str) -> bytes:
    data = text.encode()
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(data)) + data


class DemuxTests(unittest.TestCase):
    def test_multiplexed_and_raw(self):
        raw = frame(1, "2026-10-01T12:00:00.000000001Z hello\n") + frame(2, "2026-10-01T12:00:01Z oops\n")
        self.assertEqual(logs.demux(raw), ["2026-10-01T12:00:00.000000001Z hello",
                                           "2026-10-01T12:00:01Z oops"])
        self.assertEqual(logs.demux(b"tty line one\ntty line two\n"), ["tty line one", "tty line two"])

    def test_since(self):
        self.assertEqual(logs._since("2026-10-01T12:00:00.5Z x"), "1790856000.500000000")
        self.assertEqual(logs._since("2026-10-01T12:00:00Z x"), "1790856000.000000000")
        self.assertIsNone(logs._since("no timestamp"))


class ContainerLogsTests(unittest.TestCase):
    def setUp(self):
        self.bodies = [frame(1, "2026-10-01T12:00:00Z a\n") + frame(1, "2026-10-01T12:00:01Z b\n")]
        self.fake = FakeEngine(routes={("GET", "/containers/web/logs"): lambda p, b: (200, self.bodies.pop(0) if self.bodies else b"")})
        self.addCleanup(self.fake.close)
        p = patch.object(executor, "_engine", return_value=engine.EngineClient(self.fake.path))
        p.start()
        self.addCleanup(p.stop)

    def test_tail_returns_lines_and_caps(self):
        with patch.object(logs, "_start_tail") as thread:
            out = logs._container_logs({"container_name": "web", "tail": 99999}, _CFG)
        self.assertEqual(out.data, {"lines": 2})
        self.assertIn("tail=2000", [p for m, p, b in self.fake.requests if "/logs" in p][-1])
        thread.assert_not_called()

    def test_session_starts_a_tail_and_bad_params_refuse(self):
        with patch.object(logs, "_start_tail") as thread:
            logs._container_logs({"container_name": "web", "session": SESSION}, _CFG)
        thread.assert_called_once()
        for params in ({"container_name": "web", "session": "x; rm"}, {"container_name": "-x"},
                       {"container_name": "web", "tail": "lots"}):
            with self.assertRaises(ValueError):
                logs._container_logs(params, _CFG)

    def test_loop_ships_new_lines_and_stops_when_told(self):
        self.bodies = [frame(1, "2026-10-01T12:00:01Z b\n") + frame(1, "2026-10-01T12:00:02Z c\n"), b""]
        shipped = []
        answers = iter([True, False])
        with patch.object(client, "post_log_lines",
                          side_effect=lambda cfg, s, lines: shipped.append(lines) or next(answers)):
            polls = logs._tail_loop(_CFG, "web", SESSION, "2026-10-01T12:00:01Z b", sleep=lambda s: None)
        self.assertEqual(polls, 2)
        self.assertEqual(shipped, [["2026-10-01T12:00:02Z c"], []])
        log_calls = [p for m, p, b in self.fake.requests if "/logs" in p]
        self.assertIn("since=1790856001.000000000", log_calls[0])

    def test_loop_has_a_ceiling(self):
        ticks = iter(range(0, 10_000, 100))
        with patch.object(client, "post_log_lines", return_value=True):
            polls = logs._tail_loop(_CFG, "web", SESSION, None, sleep=lambda s: None,
                                    clock=lambda: next(ticks))
        self.assertLessEqual(polls, logs.MAX_SECONDS // 100 + 1)

    def test_registered(self):
        self.assertIn("container_logs", _ALL_ACTIONS)
        self.assertIs(executor._HANDLERS["container_logs"], logs._container_logs)


if __name__ == "__main__":
    unittest.main()
