"""Windows service host for the agent.

Why this exists: `sc.exe create` pointing at a plain console executable makes a
service that can never start. Windows' SCM launches the process and waits for
it to call StartServiceCtrlDispatcher; a console app never does, so after about
30 seconds the SCM gives up with

    [SC] StartService FAILED 1053:
    The service did not respond to the start or control request in a timely
    fashion.

install.ps1 created exactly that service, so the Windows agent could not run as
installed. It was never caught because no Windows binary existed to try it
with — the installer "succeeded" and the failure only appeared at Start-Service.

The agent's own loop already cooperates with shutdown: `__main__._shutdown` is
checked by the loop and by `_sleep_interruptible`. So the service does not need
to interrupt anything violently — it sets that flag and waits for the loop to
notice, which is the same path SIGTERM takes on Linux.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("vigil.winservice")

try:  # pragma: no cover - Windows only
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    _HAVE_PYWIN32 = True
except ImportError:  # pragma: no cover - every non-Windows platform
    # This module is imported by __main__ on all platforms so the --service
    # flag can report a useful error rather than an ImportError traceback.
    _HAVE_PYWIN32 = False
    win32serviceutil = None  # type: ignore[assignment]

    class _Base:  # minimal stand-in so the class body below still parses
        pass


_BASE = win32serviceutil.ServiceFramework if _HAVE_PYWIN32 else object

#: Must match the name install.ps1 passes to `sc.exe create`. The SCM matches
#: on this, and a mismatch is another 1053 that looks identical to the bug this
#: module fixes.
SERVICE_NAME = "vigil-agent"
SERVICE_DISPLAY_NAME = "Vigil Monitoring Agent"


class VigilAgentService(_BASE):  # type: ignore[misc,valid-type]
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY_NAME
    _svc_description_ = (
        "Vigil agent - outbound-only monitoring and managed tasks."
    )

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._worker: threading.Thread | None = None

    def SvcStop(self):
        """Asked to stop. Tell the loop, then let it finish the cycle.

        Reported as STOP_PENDING first: a check-in in flight can take seconds,
        and a service that claims to have stopped while its thread is still
        posting metrics is lying to the SCM.
        """
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        from . import __main__ as agent_main

        agent_main.request_shutdown()
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        from . import __main__ as agent_main

        def _run():
            try:
                agent_main.run_agent()
            except Exception:
                # Without this the thread dies silently and the service sits
                # there reporting RUNNING while doing nothing at all.
                logger.exception("Vigil agent loop exited with an exception")
                servicemanager.LogErrorMsg(
                    "Vigil agent loop exited with an exception; see the agent log."
                )
                win32event.SetEvent(self._stop_event)

        self._worker = threading.Thread(
            target=_run, name="vigil-agent-loop", daemon=True)
        self._worker.start()

        win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)

        # Give the loop a moment to leave its current cycle cleanly.
        if self._worker.is_alive():
            self._worker.join(timeout=30)


def run_service() -> int:
    """Hand this process to the SCM. Returns an exit code on failure."""
    if not _HAVE_PYWIN32:
        print(
            "ERROR: --service needs pywin32, which is not available in this "
            "build. Run the agent without --service to use it as a console "
            "application."
        )
        return 1
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(VigilAgentService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0
