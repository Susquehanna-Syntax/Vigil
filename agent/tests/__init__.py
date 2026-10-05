"""Agent test package.

Safety net: while the agent tests run, launching a real system-changing
command raises instead of running. The agent's handlers shell out to
shutdown, systemctl, docker, package managers and user tools; every test is
meant to replace those calls with a mock, and a mock that silently stops
applying (a patch on a name that moved to another module, say) would
otherwise reboot or reconfigure the machine running the suite. Read-only
helpers (sh, printf, dpkg-query, rpm, …) are not blocked.
"""
from tests import _safety_net  # noqa: F401,E402 — installs the guard
