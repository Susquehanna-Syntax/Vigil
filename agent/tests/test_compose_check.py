"""The agent checks compose's *resolved* configuration before `up` (2026-10-08).
The server checks the text; compose re-reads it (variables, include/extends,
path and boolean normalisation), so the check that counts is on what will run."""
import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import composecheck, executor  # noqa: F401 — executor first, it imports stacks
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

    def test_podman_shapes_are_checked_too(self):
        """podman-compose's `config` returns the file much as written, so the
        short and string forms must be caught as well as docker's normal form."""
        cases = {
            "short bind": _cfg(volumes=["/etc:/e"]),
            "short relative escape": _cfg(volumes=["../../../../etc:/e"]),
            "short home": _cfg(volumes=["~:/h"]),
            "dict bind without type": _cfg(volumes=[{"source": "/root", "target": "/r"}]),
            "odd volume type": _cfg(volumes=[{"type": "npipe", "source": "x", "target": "/x"}]),
            "privileged string": _cfg(privileged="true"),
            "privileged yes": _cfg(privileged="Yes"),
            "cap string": _cfg(cap_add="SYS_ADMIN"),
            "sysctl list": _cfg(sysctls=["kernel.core_pattern=|/tmp/x"]),
            "device short": _cfg(devices=["/dev/sda:/dev/sda:rwm"]),
            "device escape": _cfg(devices=["/dev/dri/../sda"]),
            "unknown key": _cfg(storage_opt={"size": "1G"}),
            "include left over": {**_cfg(), "include": ["/etc/other.yml"]},
            "network container": _cfg(network_mode="container:portainer"),
            "seccomp profile": _cfg(security_opt=["seccomp=/tmp/allow-all.json"]),
            "selinux spc": _cfg(security_opt=["label=type:spc_t"]),
            "additional context": _cfg(build={"context": WD, "additional_contexts": {"x": "/etc"}}),
            "additional context list": _cfg(build={"context": WD, "additional_contexts": ["x=/"]}),
            "dockerfile outside": _cfg(build={"context": WD, "dockerfile": "/etc/shadow"}),
            "env_file outside": _cfg(env_file=["/etc/shadow"]),
            "env_file in home": _cfg(env_file=["~/.ssh/id_rsa"]),
            "build in home": _cfg(build="~/src"),
            "variable left in a bind": _cfg(volumes=["$$HOME:/h"]),
            "variable privileged": _cfg(privileged="${P}"),
            "relative device driver": {**_cfg(), "volumes": {"v": {"driver_opts": {"type": "none", "o": "bind", "device": "etc"}}}},
        }
        for label, cfg in cases.items():
            with self.subTest(label):
                self.assertTrue(composecheck.problems(cfg, WD), label)

    def test_ordinary_stacks_still_pass(self):
        # docker's resolved JSON for a normal stack, captured on the test VM 2026-10-08
        cfg = {"name": "cc", "networks": {"default": {"name": "cc_default", "ipam": {}}},
               "services": {"a": {"cap_add": ["NET_ADMIN"], "command": None, "entrypoint": None,
                                  "environment": {"A": "1"}, "image": "nginx", "networks": {"default": None},
                                  "ports": [{"mode": "ingress", "target": 80, "published": "8080", "protocol": "tcp"}],
                                  "restart": "unless-stopped", "sysctls": {"net.core.somaxconn": "1024"},
                                  "volumes": [{"type": "bind", "source": WD + "/data", "target": "/d",
                                               "bind": {"create_host_path": True}},
                                              {"type": "bind", "source": "/srv/x", "target": "/x", "read_only": True},
                                              {"type": "volume", "source": "named", "target": "/n", "volume": {}}]},
                            "b": {"build": {"context": WD + "/sub", "dockerfile": "Dockerfile"},
                                  "depends_on": {"a": {"condition": "service_started", "required": True}},
                                  "devices": [{"source": "/dev/fuse", "target": "/dev/fuse", "permissions": "rwm"}],
                                  "volumes_from": ["a"], "privileged": False,
                                  "security_opt": ["no-new-privileges:true"]}},
               "volumes": {"named": {"name": "cc_named"}}}
        self.assertEqual(composecheck.problems(cfg, WD), [])
        self.assertEqual(composecheck.problems(_cfg(volumes=["./data:/d", "named:/n", "/anon"],
                                                    network_mode="host", sysctls=["net.ipv4.ip_forward=1"]), WD), [])

    def test_relative_paths_are_pinned_to_the_stack_folder(self):
        """`up` reads the checked file from the agent's data dir; a relative
        path left in it would resolve there (../ = the agent's own state)."""
        cfg = composecheck.pin(_cfg(volumes=["./data:/d", {"type": "bind", "source": "../x", "target": "/x"}],
                                    env_file=".env", build="./src",
                                    ), WD)
        svc = cfg["services"]["a"]
        self.assertEqual(svc["volumes"][0], WD + "/data:/d")
        self.assertEqual(svc["volumes"][1]["source"], "/opt/vigil/stacks/x")
        self.assertEqual(svc["env_file"], [WD + "/.env"])
        self.assertEqual(svc["build"]["context"], WD + "/src")

    def test_a_variable_anywhere_that_matters_is_refused(self):
        self.assertTrue(composecheck.problems(_cfg(tmpfs=["/run/${X}"]), WD))
        self.assertTrue(composecheck.problems(_cfg(networks={"${NET}": None}), WD))
        self.assertTrue(composecheck.problems(_cfg(ulimits={"nofile": "$N"}), WD))
        # where it can only reach inside the container, docker's $$ stays fine
        self.assertEqual(composecheck.problems(_cfg(environment={"A": "x$$y"}, command=["sh", "-c", "echo $$HOME"],
                                                    labels={"l": "$$"}), WD), [])

    def test_a_bind_inside_a_writable_bind_is_refused(self):
        """A container that can write a folder can swap a symlink into it
        between the check and the mount."""
        self.assertTrue(composecheck.problems(_cfg(volumes=["./:/stack", "./data:/d"]), WD))
        self.assertTrue(composecheck.problems(_cfg(volumes=["./data:/d"]), WD, live_rw=(WD,)))
        self.assertTrue(composecheck.problems(_cfg(volumes=["/srv/a/b:/d"]), WD, live_rw=("/srv/a",)))
        # read-only, or the same folder, cannot plant anything
        self.assertEqual(composecheck.problems(_cfg(volumes=["./:/stack:ro", "./data:/d"]), WD), [])
        self.assertEqual(composecheck.problems(_cfg(volumes=["./data:/d"]), WD, live_rw=(WD + "/data",)), [])

    def test_a_symlink_already_in_the_stack_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "real"))
            os.symlink(os.path.join(tmp, "real"), os.path.join(tmp, "data"))
            self.assertTrue(composecheck.problems(_cfg(volumes=["./data/sub:/d"]), tmp))

    def test_a_symlink_planted_in_the_stack_folder_is_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink("/etc", os.path.join(tmp, "data"))
            self.assertTrue(composecheck.problems(_cfg(volumes=["./data:/d"]), tmp))
            self.assertTrue(composecheck.problems(_cfg(build={"context": tmp + "/data"}), tmp))


class DeployRefusesTests(unittest.TestCase):
    def test_deploy_stops_before_up_when_the_resolved_config_escapes(self):
        resolved = json.dumps(_cfg(volumes=[{"type": "bind", "source": "/etc", "target": "/e"}]))
        calls = []

        def fake_compose(workdir, project, *args, **kw):
            calls.append(args)
            return resolved if args[:1] == ("config",) else "up ok"
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(stacks, "_compose", side_effect=fake_compose), \
                patch.object(stacks, "_live_rw_binds", return_value=()), \
                patch.object(stacks.client if hasattr(stacks, "client") else stacks, "fetch_stack_env", return_value="", create=True):
            with self.assertRaises(ValueError):
                stacks._check_resolved(Path(tmp), "web", "compose.yaml", Path(tmp))
        self.assertFalse([c for c in calls if c[:1] == ("up",)])

    def test_live_writable_binds_come_from_vigil_stacks_only(self):
        engine = unittest.mock.Mock()
        engine.get.return_value = [
            {"Labels": {"com.docker.compose.project.config_files": "/data/stacks/web.json"},
             "Mounts": [{"Type": "bind", "Source": "/srv/web", "RW": True},
                        {"Type": "bind", "Source": "/srv/ro", "RW": False},
                        {"Type": "volume", "Source": "/var/lib/docker/volumes/x", "RW": True}]},
            {"Labels": {"com.docker.compose.project.config_files": "/home/admin/compose.yml"},
             "Mounts": [{"Type": "bind", "Source": "/home", "RW": True}]},
            {"Labels": {}, "Mounts": [{"Type": "bind", "Source": "/", "RW": True}]},
        ]
        with patch.object(stacks.ex, "_engine", return_value=engine):
            self.assertEqual(stacks._live_rw_binds(Path("/data")), ("/srv/web",))

    def test_unreadable_config_refuses(self):
        with patch.object(stacks, "_compose", side_effect=RuntimeError("compose missing")):
            with self.assertRaises(ValueError):
                stacks._check_resolved(Path("/tmp"), "web", "compose.yaml", Path("/tmp"))


if __name__ == "__main__":
    unittest.main()
