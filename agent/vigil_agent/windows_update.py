"""Windows Update backend over the Windows Update Agent (WUA) COM API.

Same shape as firewall.py: an abstract backend, one concrete backend, a
platform ``available()`` test and a module-level ``detect()``. The COM layer
is the only part that needs a Windows host; it is isolated behind a
``session_factory`` seam so tests (and future non-COM transports) can
substitute fakes. ``win32com``/``pythoncom`` are imported lazily inside the
backend, so this module imports cleanly on Linux with pywin32 absent.

scan() returns plain dicts so the result is JSON-serializable for the task
result; install() re-runs the search and filters by update id rather than
trusting a caller to hand back COM objects.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod

# IUpdate.MsrcSeverity -> rank. Explicit dict, never string comparison: a
# naive `>=` on the raw names would pass "Moderate" through an "important"
# floor. An empty MsrcSeverity means "no severity" and is ranked below the
# lowest floor, so it is dropped whenever a floor is set.
SEVERITY_RANKS = {
    "": 0,
    "low": 1,
    "moderate": 2,
    "important": 3,
    "critical": 4,
}

#: Default Search() criteria: pending, software only (no drivers), and not
#: hidden by an administrator.
DEFAULT_CRITERIA = "IsInstalled=0 AND Type='Software' AND IsHidden=0"

#: install() result codes (OperationResultCode).
RESULT_NOT_STARTED = 0
RESULT_IN_PROGRESS = 1
RESULT_SUCCEEDED = 2
RESULT_SUCCEEDED_WITH_ERRORS = 3
RESULT_FAILED = 4
RESULT_ABORTED = 5

_INSTALL_RESULT_CODES = {
    RESULT_NOT_STARTED: "not_started",
    RESULT_IN_PROGRESS: "in_progress",
    RESULT_SUCCEEDED: "succeeded",
    RESULT_SUCCEEDED_WITH_ERRORS: "succeeded_with_errors",
    RESULT_FAILED: "failed",
    RESULT_ABORTED: "aborted",
}


def normalize_kb(value: str) -> str:
    """Normalize a KB identifier to its bare numeric form (``KB5034123`` ->
    ``5034123``). Empty strings normalize to the empty string, which matches
    nothing (an update with no KB is never in a KB list)."""
    value = str(value or "").strip().upper()
    if value.startswith("KB"):
        value = value[2:]
    return value


def filter_updates(updates, classifications=None, include_kb=None,
                   exclude_kb=None, severity_floor=None) -> list[dict]:
    """Filter plain update dicts (the shape scan() returns) without any COM.

    - ``classifications``: category names, matched case-insensitively against
      each update's ``categories``; an update matching any listed name passes.
      Empty/None means no classification filter.
    - ``include_kb``: when non-empty, only updates whose KB is listed pass.
      Both ``KB5034123`` and ``5034123`` forms are accepted.
    - ``exclude_kb``: same normalization; a matching update is dropped.
      Applied after ``include_kb`` and wins on conflict -- an update named in
      both lists is excluded, the safer reading of an ambiguous intent.
    - ``severity_floor``: one of ``low``/``moderate``/``important``/``critical``.
      Ranks via SEVERITY_RANKS, never string comparison. An update with no
      severity (empty ``severity``) is dropped when a floor is set.
    """
    if severity_floor is not None:
        floor = str(severity_floor).strip().lower()
        if floor not in SEVERITY_RANKS or floor == "":
            raise ValueError(
                f"Unknown severity floor {severity_floor!r}; expected one of "
                "low, moderate, important, critical")
    classifications = [str(c).lower() for c in (classifications or [])]
    include = {normalize_kb(k) for k in (include_kb or [])}
    exclude = {normalize_kb(k) for k in (exclude_kb or [])}

    out = []
    for u in updates:
        if classifications:
            cats = {str(c).lower() for c in u.get("categories") or []}
            if not (cats & set(classifications)):
                continue
        kb = normalize_kb(u.get("kb"))
        if include and kb not in include:
            continue
        if kb in exclude:
            continue
        if severity_floor is not None:
            rank = SEVERITY_RANKS.get(str(u.get("severity", "")).lower())
            if rank is None or rank < SEVERITY_RANKS[floor]:
                continue
        out.append(u)
    return out


class WindowsUpdateBackend(ABC):
    name = ""

    @staticmethod
    @abstractmethod
    def available() -> bool:
        raise NotImplementedError

    @abstractmethod
    def scan(self, criteria_extra: str = "") -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def install(self, update_ids: list[str]) -> dict:
        raise NotImplementedError


def _default_session_factory():
    import win32com.client
    return win32com.client.Dispatch("Microsoft.Update.Session")


class WuaBackend(WindowsUpdateBackend):
    name = "windows_update"

    def __init__(self, session_factory=None):
        # The COM seam: tests inject a factory returning a fake session so no
        # test imports win32com. The default factory is a closure (not a bare
        # lambda) because it must import win32com lazily at call time, not at
        # module import time.
        self._session_factory = session_factory or _default_session_factory

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def _com_runtime(self):
        # Returns a context manager that initializes COM on the calling
        # thread, or a null context when pythoncom is absent (a non-Windows
        # host running against an injected fake session). COM must be
        # initialized on the calling thread: the agent may run a handler on a
        # worker thread where it is not, and the Dispatch call then fails
        # with a confusing "CoInitialize has not been called" error.
        # Reentrant: on Windows a second CoInitialize on an already-
        # initialized thread is a no-op and is paired with a CoUninitialize,
        # so the thread's state is preserved. The import is lazy (never at
        # module top level) so this module imports cleanly on Linux with
        # pywin32 absent; on a Windows host missing pywin32 the failure is
        # loud, from the session factory.
        import contextlib

        try:
            import pythoncom
        except ImportError:
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def _init():
            pythoncom.CoInitialize()
            try:
                yield
            finally:
                pythoncom.CoUninitialize()

        return _init()

    def _search(self, session, criteria: str) -> list:
        searcher = session.CreateUpdateSearcher()
        result = searcher.Search(criteria)
        return list(result.Updates)

    def _to_dict(self, update) -> dict:
        kba = list(update.KBArticleIDs or [])
        kb = f"KB{kba[0]}" if len(kba) == 1 else ""
        return {
            "update_id": str(update.Identity.UpdateID),
            "title": str(update.Title),
            "kb": kb,
            "severity": str(update.MsrcSeverity or "").lower(),
            "categories": [str(c.Name) for c in (update.Categories or [])],
            "reboot_required": bool(update.RebootRequired),
            "is_downloaded": bool(update.IsDownloaded),
            "is_mandatory": bool(update.IsMandatory),
        }

    def scan(self, criteria_extra: str = "") -> list[dict]:
        criteria = (
            f"{DEFAULT_CRITERIA} AND ({criteria_extra})"
            if criteria_extra else DEFAULT_CRITERIA
        )
        with self._com_runtime():
            return [self._to_dict(u)
                    for u in self._search(self._session_factory(), criteria)]

    def install(self, update_ids: list[str]) -> dict:
        wanted = [str(i) for i in (update_ids or [])]
        with self._com_runtime():
            session = self._session_factory()
            found = {u.Identity.UpdateID: u
                     for u in self._search(session, DEFAULT_CRITERIA)}
            # An id that matches no pending update is reported in ``failed``,
            # never silently skipped.
            missing = [i for i in wanted if i not in found]
            coll = session.CreateUpdateCollection()
            order = []
            for uid in wanted:
                update = found.get(uid)
                if update is None:
                    continue
                if not update.EulaAccepted:
                    update.AcceptEula()
                coll.Add(update)
                order.append(uid)
            if not order:
                return {"result_code": RESULT_NOT_STARTED,
                        "reboot_required": False, "installed": [],
                        "failed": missing, "detail": "no matching updates"}
            downloader = session.CreateUpdateDownloader()
            downloader.Updates = coll
            downloader.Download()
            installer = session.CreateUpdateInstaller()
            installer.Updates = coll
            result = installer.Install()
            installed = []
            failed = list(missing)
            for i, uid in enumerate(order):
                code = int(result.GetUpdateResult(i).ResultCode)
                if code in (RESULT_SUCCEEDED, RESULT_SUCCEEDED_WITH_ERRORS):
                    installed.append(uid)
                else:
                    failed.append(uid)
            detail = _INSTALL_RESULT_CODES.get(int(result.ResultCode),
                                               "unknown")
            return {"result_code": int(result.ResultCode),
                    "reboot_required": bool(result.RebootRequired),
                    "installed": installed, "failed": failed,
                    "detail": detail}


def detect() -> WindowsUpdateBackend | None:
    """Return the backend for this host, or None if Windows Update is
    unavailable (i.e. not on Windows)."""
    if WuaBackend.available():
        return WuaBackend()
    return None
