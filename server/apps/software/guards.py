"""Which hosts can act on a package source, as a task ``when:`` guard.

An app offered by several package managers (Firefox: winget on Windows,
dpkg on Ubuntu, Flathub anywhere) becomes one step per manager. Without a
guard, every host fails the steps for the managers it does not have: the
agent refuses a winget step on Linux and a dpkg step on Fedora. With one,
the step is skipped there instead. The names are the agent's ``when``
context (agent/vigil_agent/__main__.py ``_build_when_context``) and its
manager → source map (actions/apps.py ``_MANAGER_SOURCE``).
"""

_GUARDS = {
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


def source_guard(source: str) -> str:
    """The ``when:`` for a step acting through ``source``; "" for the host's own."""
    return _GUARDS.get(source or "", "")
