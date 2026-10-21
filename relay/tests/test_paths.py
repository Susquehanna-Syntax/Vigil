"""The relay's forward allowlist must be the dashboard's, byte for byte."""

import ast
import pathlib
import unittest

from vigil_relay import paths

DASHBOARD_TRUST = pathlib.Path(__file__).resolve().parents[2] / "server" / "apps" / "relays" / "trust.py"


def _dashboard_tuples():
    tree = ast.parse(DASHBOARD_TRUST.read_text())
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in ("AGENT_PATHS", "AGENT_PREFIXES"):
                    found[target.id] = ast.literal_eval(node.value)
    return found


class PathsTest(unittest.TestCase):
    def test_lists_match_the_dashboard(self):
        self.assertTrue(DASHBOARD_TRUST.is_file(), f"missing {DASHBOARD_TRUST}")
        dashboard = _dashboard_tuples()
        self.assertEqual(sorted(dashboard), ["AGENT_PATHS", "AGENT_PREFIXES"])
        self.assertEqual(list(paths.AGENT_PATHS), list(dashboard["AGENT_PATHS"]))
        self.assertEqual(list(paths.AGENT_PREFIXES), list(dashboard["AGENT_PREFIXES"]))

    def test_ws_prefixes(self):
        self.assertEqual(paths.WS_PREFIXES, ("/ws/agent/",))

    def test_allowed(self):
        yes = [
            ("POST", "/api/v1/checkin"),
            ("POST", "/api/v1/register"),
            ("GET", "/agent/install.sh"),
            ("GET", "/agent/info/"),
            ("GET", "/api/v1/tasks/result/"),
            ("GET", "/api/v1/agent/metrics"),
            ("GET", "/agent/download/payload.exe"),
            ("HEAD", "/api/v1/checkin"),
        ]
        for method, path in yes:
            self.assertTrue(paths.allowed(method, path), (method, path))

        no = [
            ("GET", "/api/v1/hosts/"),
            ("GET", "/"),
            ("GET", "/admin/"),
            ("GET", "/login/"),
            ("GET", "/agent/download/../x"),
            ("DELETE", "/api/v1/checkin"),
            ("PUT", "/api/v1/checkin"),
            ("POST", "/api/v1/tasks/result/extra"),
            ("GET", "/relay/uplink"),
        ]
        for method, path in no:
            self.assertFalse(paths.allowed(method, path), (method, path))

    def test_split_tenant(self):
        tenants = {"acme": "0" * 64, "default": "1" * 64}
        self.assertEqual(paths.split_tenant("/t/acme/api/v1/checkin", tenants),
                         ("acme", "/api/v1/checkin"))
        self.assertEqual(paths.split_tenant("/t/acme/", tenants), ("acme", "/"))
        self.assertEqual(paths.split_tenant("/t/globex/api/v1/checkin", tenants), (None, "/t/globex/api/v1/checkin"))
        self.assertEqual(paths.split_tenant("/api/v1/checkin", tenants), ("default", "/api/v1/checkin"))

    def test_split_tenant_without_a_default_tenant(self):
        tenants = {"acme": "0" * 64}
        self.assertEqual(paths.split_tenant("/api/v1/checkin", tenants), (None, "/api/v1/checkin"))
        self.assertEqual(paths.split_tenant("/t/acme/relay/uplink", tenants), ("acme", "/relay/uplink"))


if __name__ == "__main__":
    unittest.main()
