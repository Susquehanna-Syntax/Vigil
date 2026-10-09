"""The agent checks compose's *resolved* configuration before `up` (2026-10-08).
The server checks the text; compose re-reads it (variables, include/extends,
path and boolean normalisation), so the check that counts is on what will run."""
import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import composecheck
from vigil_agent.actions import stacks

WD = "/opt/vigil/stacks/web"


def _cfg(**svc):
    return {"services": {"a": {"image": "x", **svc}}}


class ProblemsTests(unittest.TestCase):
    def test_a_plain_service_is_fine(self):
        self.assertEqual(composecheck.problems(_cfg(volumes=[{"type": "bind", "source": "/srv/data", "target": "/d"}]), WD), [])

    def test_resolved_escapes_are_found(self):
        cases = {
            "interpolated bind": _cfg(volumes=[{"type": "bind", "source": "/etc", "target": "/e"}]),
            "double slash": _cfg(volumes=[{"type": "bind", "source": "//run", "target": "/r"}]),
            "ancestor of socket": _cfg(volumes=[{"type": "bind", "source": "/var", "target": "/v"}]),
            "privileged": _cfg(privileged=True),
            "pid host": _cfg(pid="host"),
            "cap": _cfg(cap_add=["CAP_SYS_ADMIN"]),
            "device": _cfg(devices=[{"source": "/dev/sda", "target": "/dev/sda"}]),
            "api socket": _cfg(use_api_socket=True),
            "volumes_from container": _cfg(volumes_from=["container:portainer"]),
            "build outside": _cfg(build={"context": "/"}),
            "secret outside": {**_cfg(), "secrets": {"s": {"file": "/etc/shadow"}}},
            "driver_opts device": {**_cfg(), "volumes": {"v": {"driver_opts": {"device": "/etc"}}}},
        }
        for label, cfg in cases.items():
            with self.subTest(label):
                self.assertTrue(composecheck.problems(cfg, WD))


class DeployRefusesTests(unittest.TestCase):
    def test_deploy_stops_before_up_when_the_resolved_config_escapes(self):
        resolved = json.dumps(_cfg(volumes=[{"type": "bind", "source": "/etc", "target": "/e"}]))
        calls = []

        def fake_compose(workdir, project, *args, **kw):
            calls.append(args)
            return resolved if args[:1] == ("config",) else "up ok"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(stacks, "_compose", side_effect=fake_compose), \
                patch.object(stacks.client if hasattr(stacks, "client") else stacks, "fetch_stack_env", return_value="", create=True):
            with self.assertRaises(ValueError):
                stacks._check_resolved(Path(tmp), "web", "compose.yaml")
        self.assertFalse([c for c in calls if c[:1] == ("up",)])

    def test_unreadable_config_refuses(self):
        with patch.object(stacks, "_compose", side_effect=RuntimeError("compose missing")):
            with self.assertRaises(ValueError):
                stacks._check_resolved(Path("/tmp"), "web", "compose.yaml")


if __name__ == "__main__":
    unittest.main()
