"""The agent-test safety net (see tests/__init__.py for why).

Imported by the package *and* by every test module, so a test file run on
its own — or imported by a one-off script — still cannot launch a real
system-changing command. Idempotent: patching twice is a no-op.
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


if getattr(subprocess.Popen.__init__, "_vigil_guard", False) is False:
    _guarded_popen_init._vigil_guard = True
    subprocess.Popen.__init__ = _guarded_popen_init
