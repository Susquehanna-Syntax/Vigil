"""The server's per-source step guards (server/apps/software/guards.py)
evaluated by the agent's own ``when`` evaluator: each source's step runs on
the hosts that have that package manager and is skipped everywhere else.
The server's test_source_guards checks this table names every guard it has."""
import tests._safety_net  # noqa: F401
import unittest

from vigil_agent.expression import evaluate

GUARDS = {
    "dpkg": 'agent.pkg_manager in ["apt", "apt-get"]',
    "rpm": 'agent.pkg_manager in ["dnf", "yum", "zypper"]',
    "apk": 'agent.pkg_manager == "apk"',
    "pacman": 'agent.pkg_manager == "pacman"',
    "flatpak": 'agent.os == "linux"',
    "snap": 'agent.os == "linux"',
    "winget": 'agent.os == "windows"',
    "chocolatey": 'agent.os == "windows"',
    "scoop": 'agent.os == "windows"',
    "registry": 'agent.os == "windows"',
}

HOSTS = {   # agent context → the sources whose steps should run there
    "ubuntu": ({"os": "linux", "pkg_manager": "apt"}, {"dpkg", "flatpak", "snap"}),
    "fedora": ({"os": "linux", "pkg_manager": "dnf"}, {"rpm", "flatpak", "snap"}),
    "alpine": ({"os": "linux", "pkg_manager": "apk"}, {"apk", "flatpak", "snap"}),
    "arch": ({"os": "linux", "pkg_manager": "pacman"}, {"pacman", "flatpak", "snap"}),
    "windows": ({"os": "windows", "pkg_manager": "winget"}, {"winget", "chocolatey", "scoop", "registry"}),
    "mac": ({"os": "darwin", "pkg_manager": "brew"}, set()),
}


class SourceGuardTests(unittest.TestCase):
    def test_each_step_runs_only_where_its_manager_is(self):
        for host, (agent, expected) in HOSTS.items():
            runs = {src for src, guard in GUARDS.items()
                    if evaluate(guard, {"agent": {**agent, "arch": "amd64"}, "inputs": {}, "host": {}})}
            self.assertEqual(runs, expected, host)


if __name__ == "__main__":
    unittest.main()
