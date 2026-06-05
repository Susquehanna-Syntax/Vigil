"""Scanner ABC — shared shape every vulnerability scanner implements.

The contract is intentionally small. A scanner:

  * advertises a stable ``name`` used as the value of ``VulnScan.scanner``,
  * reports whether it's ``configured()`` (env vars present, creds valid
    shape, etc.) so the periodic sync can skip it silently when not in use,
  * implements ``sync()`` which performs one full cycle (launch any
    pending scans, poll in-flight, ingest completed results).

Scanners that are event-driven rather than poll-driven (Trivy, where the
agent runs the scan and POSTs results) implement ``sync()`` as a no-op and
expose a separate ingest entry point called from the task-completion
handler. Keeping the ABC small lets each impl decide its own model
without forcing the agent-push case to pretend it has a launch step.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class Scanner(ABC):
    #: Stable identifier used as ``VulnScan.scanner`` and in alert messages.
    name: ClassVar[str]

    @abstractmethod
    def configured(self) -> bool:
        """Return True when the scanner has all required configuration.

        The periodic sync calls this before ``sync()`` and silently skips
        scanners that report False. Implementations should be cheap — just
        check env vars / settings, not reach out to the scanner.
        """

    @abstractmethod
    def sync(self) -> str:
        """Run one sync cycle.

        For poll-driven scanners (Nessus, Greenbone) this launches pending
        scans, polls in-flight ones, and ingests completed results.

        For event-driven scanners (Trivy) this is a no-op — results land
        via the task-completion handler instead.

        Returns a short human-readable status line for logging.
        """
