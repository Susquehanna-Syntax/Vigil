"""hunt_process, hunt_port, hunt_service: the three remaining read-only hunts."""
import json
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, hunt
from vigil_agent.config import AgentConfig


def _conn(port, proto="tcp", status=psutil.CONN_LISTEN, ip="127.0.0.1", pid=42, family=socket.AF_INET):
    sock_type = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
    laddr = SimpleNamespace(family=family, ip=ip, port=port)
    raddr = SimpleNamespace(family=family, ip="0.0.0.0", port=0)
    return SimpleNamespace(fd=1, family=family, type=sock_type, laddr=laddr,
                           raddr=raddr, status=status, pid=pid)


def _proc(pid, name, cmdline=(), username="root", create_time=1700000000.0):
    return SimpleNamespace(info={"pid": pid, "name": name, "cmdline": list(cmdline),
                                 "username": username, "create_time": create_time})


class HuntProcessTests(unittest.TestCase):
    def test_process_by_name_cmdline_user(self):
        procs = [
            _proc(1, "java", ["java", "-jar", "/opt/APP.jar"], "deploy"),
            _proc(2, "sshd", ["/usr/sbin/sshd", "-D"], "root"),
            _proc(3, "python3", ["python3", "agent.py"], "root"),
        ]
        with patch.object(hunt.psutil, "process_iter", return_value=iter(procs)):
            out = hunt.run_hunt(hunt.hunt_process, {"name": "java"})
        body = json.loads(out)
        self.assertEqual(body["matches"], [
            {"evidence_type": "process", "name": "java", "pid": 1, "cmdline": "java -jar /opt/APP.jar",
             "user": "deploy", "started": "2023-11-14T22:13:20Z"}])
        self.assertEqual(out.data, {"matched": True, "count": 1, "truncated": False})

        with patch.object(hunt.psutil, "process_iter", return_value=iter(procs)):
            out = hunt.run_hunt(hunt.hunt_process, {"cmdline": "app.Jar"})
        self.assertEqual(out.data["count"], 1)  # case-insensitive substring

        with patch.object(hunt.psutil, "process_iter", return_value=iter(procs)):
            out = hunt.run_hunt(hunt.hunt_process, {"user": "root"})
        self.assertEqual(out.data["count"], 2)

    def test_process_access_denied_is_skipped(self):
        ok = _proc(1, "java", ["java"], "deploy")

        class Dead:
            @property
            def info(self):
                raise psutil.AccessDenied(13, "denied")

        with patch.object(hunt.psutil, "process_iter",
                          return_value=iter([Dead(), ok])):
            out = hunt.run_hunt(hunt.hunt_process, {"name": "java"})
        self.assertEqual(out.data["count"], 1)

    def test_process_needs_a_filter(self):
        with self.assertRaises(RuntimeError) as ctx:
            hunt.run_hunt(hunt.hunt_process, {})
        self.assertIn("needs name, cmdline or user", str(ctx.exception))

    def test_process_cmdline_cut_to_500(self):
        long = _proc(1, "x", ["x"] + ["a" * 100] * 10)
        with patch.object(hunt.psutil, "process_iter", return_value=iter([long])):
            out = hunt.run_hunt(hunt.hunt_process, {"name": "x"})
        self.assertEqual(len(json.loads(out)["matches"][0]["cmdline"]), 500)


class HuntPortTests(unittest.TestCase):
    def test_port_listen_only_for_tcp(self):
        conns = [
            _conn(8443, proto="tcp", status=psutil.CONN_LISTEN, pid=10),
            _conn(8443, proto="tcp", status=psutil.CONN_ESTABLISHED, pid=11),
            _conn(5353, proto="udp", status=psutil.CONN_NONE, pid=12),
        ]
        with patch.object(hunt.psutil, "net_connections", return_value=conns), \
                patch.object(hunt.psutil, "Process", side_effect=psutil.NoSuchProcess(0)):
            out = hunt.run_hunt(hunt.hunt_port, {"port": 8443, "protocol": "tcp"})
        body = json.loads(out)
        self.assertEqual(body["matches"], [
            {"evidence_type": "port", "port": 8443, "protocol": "tcp",
             "address": "127.0.0.1", "pid": 10, "process": None}])

    def test_port_range_and_process(self):
        conns = [
            _conn(8443, pid=10),
            _conn(8444, pid=10),
            _conn(8445, pid=20),
            _conn(9000, pid=20),
        ]
        procs = {10: "app-server", 20: "other"}

        class P:
            def __init__(self, pid):
                self._n = procs[pid]

            def name(self):
                return self._n

        with patch.object(hunt.psutil, "net_connections", return_value=conns), \
                patch.object(hunt.psutil, "Process", side_effect=P):
            out = hunt.run_hunt(hunt.hunt_port, {"port": "8443-8444", "process": "app*"})
        body = json.loads(out)
        self.assertEqual(sorted(m["port"] for m in body["matches"]), [8443, 8444])
        self.assertTrue(all(m["process"] == "app-server" for m in body["matches"]))

    def test_port_dedup(self):
        conns = [_conn(8443, pid=10), _conn(8443, pid=10), _conn(8443, pid=10)]
        with patch.object(hunt.psutil, "net_connections", return_value=conns), \
                patch.object(hunt.psutil, "Process", side_effect=psutil.NoSuchProcess(0)):
            out = hunt.run_hunt(hunt.hunt_port, {"port": 8443})
        self.assertEqual(out.data["count"], 1)

    def test_port_needs_port_or_process(self):
        with self.assertRaises(RuntimeError) as ctx:
            hunt.run_hunt(hunt.hunt_port, {})
        self.assertIn("needs port or process", str(ctx.exception))


class HuntServiceTests(unittest.TestCase):
    def test_service_linux_state_and_start_mode(self):
        units = [
            {"unit": "ssh.service", "state": "running", "unit_state": "enabled"},
            {"unit": "cron.service", "state": "stopped", "unit_state": "disabled"},
            {"unit": "network.service", "state": "running", "unit_state": "static"},
            {"unit": "masked-thing.service", "state": "stopped", "unit_state": "masked"},
            {"unit": "runtime-thing.service", "state": "stopped", "unit_state": "enabled-runtime"},
        ]
        with patch.object(hunt, "_systemd_services", return_value=units), \
                patch.object(sys, "platform", "linux"):
            out = hunt.run_hunt(hunt.hunt_service, {"name": "ssh", "state": "running",
                                                    "start_mode": "enabled"})
        body = json.loads(out)
        self.assertEqual(body["matches"], [
            {"evidence_type": "service", "name": "ssh", "state": "running", "start_mode": "enabled"}])

        # static / masked match only when start_mode is omitted
        with patch.object(hunt, "_systemd_services", return_value=units), \
                patch.object(sys, "platform", "linux"):
            out = hunt.run_hunt(hunt.hunt_service, {"name": "*", "start_mode": "disabled"})
        body = json.loads(out)
        self.assertEqual(body["matches"], [
            {"evidence_type": "service", "name": "cron", "state": "stopped", "start_mode": "disabled"}])
        self.assertFalse(out.data["truncated"])

    def test_service_suffix_stripped(self):
        units = [{"unit": "sshd.service", "state": "running", "unit_state": "enabled"}]
        with patch.object(hunt, "_systemd_services", return_value=units), \
                patch.object(sys, "platform", "linux"):
            out = hunt.run_hunt(hunt.hunt_service, {"name": "sshd"})
        self.assertEqual(json.loads(out)["matches"][0]["name"], "sshd")

    def test_service_windows_start_types(self):
        def svc(name, status, start_type):
            # Like psutil's WindowsService: the fields are methods.
            return SimpleNamespace(name=lambda: name, status=lambda: status,
                                   start_type=lambda: start_type)
        services = [
            svc("w32time", psutil.STATUS_RUNNING, "automatic"),
            svc("Fax", psutil.STATUS_STOPPED, "disabled"),
            svc("EventLog", psutil.STATUS_STOPPED, "manual"),
        ]
        fake_iter = type("iter", (), {"__iter__": lambda self: iter(services)})
        with patch.object(hunt.psutil, "win_service_iter", fake_iter, create=True), \
                patch.object(sys, "platform", "win32"):
            out = hunt.run_hunt(hunt.hunt_service, {"name": "*"})
        body = json.loads(out)
        self.assertEqual([(m["name"], m["state"], m["start_mode"]) for m in body["matches"]],
                         [("w32time", "running", "enabled"),
                          ("Fax", "stopped", "disabled"),
                          ("EventLog", "stopped", None)])
        # manual matches only when start_mode is omitted
        fake_iter2 = type("iter2", (), {"__iter__": lambda self: iter(services)})
        with patch.object(hunt.psutil, "win_service_iter", fake_iter2, create=True), \
                patch.object(sys, "platform", "win32"):
            out = hunt.run_hunt(hunt.hunt_service, {"name": "*", "start_mode": "enabled"})
        self.assertEqual(out.data["count"], 1)

    def test_service_needs_name(self):
        with self.assertRaises(RuntimeError):
            hunt.run_hunt(hunt.hunt_service, {})


class HuntThroughExecutorTests(unittest.TestCase):
    def test_through_execute_action(self):
        cfg = AgentConfig(server_url="https://vigil.example.com", agent_token="t",
                          mode="full_control")
        procs = [_proc(7, "java", ["java", "-jar", "app.jar"], "deploy")]
        with patch.object(hunt.psutil, "process_iter", return_value=iter(procs)):
            out = executor.execute_action("hunt_process", {"name": "java"}, cfg)
        self.assertEqual(out.data["count"], 1)

        conns = [_conn(8443, pid=7)]
        with patch.object(hunt.psutil, "net_connections", return_value=conns), \
                patch.object(hunt.psutil, "Process", side_effect=psutil.NoSuchProcess(0)):
            out = executor.execute_action("hunt_port", {"port": 8443}, cfg)
        self.assertEqual(out.data["count"], 1)

        units = [{"unit": "ssh.service", "state": "running", "unit_state": "enabled"}]
        with patch.object(hunt, "_systemd_services", return_value=units), \
                patch.object(sys, "platform", "linux"):
            out = executor.execute_action("hunt_service", {"name": "ssh"}, cfg)
        self.assertEqual(out.data["count"], 1)


if __name__ == "__main__":
    unittest.main()
