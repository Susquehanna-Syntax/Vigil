"""Agent test package.

Safety net: while the agent tests run, launching a real system-changing
command raises instead of running. The agent's handlers shell out to
shutdown, systemctl, docker, package managers and user tools; every test is
meant to replace those calls with a mock, and a mock that silently stops
applying (a patch on a name that moved to another module, say) would
otherwise reboot or reconfigure the machine running the suite. Read-only
helpers (sh, printf, dpkg-query, rpm, …) are not blocked.
"""
import os
import subprocess

_BLOCKED = frozenset({
    "shutdown", "reboot", "halt", "poweroff",
    "systemctl", "service",
    "docker", "podman",
    "apt", "apt-get", "dpkg", "dnf", "yum", "pacman", "zypper", "apk", "snap", "brew",
    "winget", "choco", "scoop",
    "useradd", "userdel", "usermod", "adduser", "deluser", "groupadd",
    "crontab", "hostnamectl",
    "ufw", "firewall-cmd", "iptables", "nft", "netsh",
    "sc", "sc.exe", "powershell", "pwsh",
    "trivy",
})

_real_popen_init = subprocess.Popen.__init__


def _program(args) -> str:
    first = args if isinstance(args, (str, bytes, os.PathLike)) else (args[0] if args else "")
    first = os.fsdecode(first)
    return os.path.basename(first.split()[0] if first else "")


def _guarded_popen_init(self, args, *a, **kw):
    prog = _program(args)
    if prog in _BLOCKED:
        raise RuntimeError(
            f"agent test tried to run a real {prog!r} command — mock it "
            f"(argv: {args!r})")
    return _real_popen_init(self, args, *a, **kw)


subprocess.Popen.__init__ = _guarded_popen_init
