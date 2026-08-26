"""Windows Update backend: the COM layer is exercised only through fakes.

No test imports win32com; WuaBackend takes a session_factory seam and the
fakes below stand in for Microsoft.Update.Session. The pure filtering
helpers are tested directly on plain dicts.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, windows_update
from vigil_agent.config import AgentConfig, _ALL_ACTIONS
from vigil_agent.windows_update import (
    RESULT_SUCCEEDED,
    filter_updates,
    normalize_kb,
)


class FakeUpdate:
    def __init__(self, update_id, title, kb_raw="", severity="",
                 categories=(), reboot_required=False, is_downloaded=False,
                 is_mandatory=False):
        self.Identity = type("Identity", (), {"UpdateID": update_id})
        self.Title = title
        self.KBArticleIDs = [kb_raw] if kb_raw else []
        self.MsrcSeverity = severity
        self.Categories = [type("Cat", (), {"Name": c})
                           for c in categories]
        self.RebootRequired = reboot_required
        self.IsDownloaded = is_downloaded
        self.IsMandatory = is_mandatory
        self.EulaAccepted = False
        self.accepted_eula = False

    def AcceptEula(self):
        self.EulaAccepted = True
        self.accepted_eula = True


class FakeCollection:
    def __init__(self):
        self.updates = []

    def Add(self, update):
        self.updates.append(update)

    def __iter__(self):
        return iter(self.updates)


class FakeResult:
    def __init__(self, updates):
        self.updates = list(updates)

    @property
    def Updates(self):
        return self.updates


class FakeSearcher:
    def __init__(self, updates, calls=None):
        self._updates = updates
        self._calls = calls

    def Search(self, criteria):
        if self._calls is not None:
            self._calls.append(criteria)
        return FakeResult(self._updates)


class FakeDownloader:
    def __init__(self, calls):
        self._calls = calls
        self.Updates = None

    def Download(self):
        self._calls.append(("download", len(self.Updates.updates)))


class FakeUpdateResult:
    def __init__(self, code):
        self.ResultCode = code


class FakeInstaller:
    def __init__(self, calls, per_update_codes=None):
        self._calls = calls
        self.Updates = None
        self._per_update = per_update_codes
        self.result_code = RESULT_SUCCEEDED
        self.reboot_required = False

    def Install(self):
        self._calls.append(("install", len(self.Updates.updates)))
        count = len(self.Updates.updates)
        per_update = (self._per_update
                      or [RESULT_SUCCEEDED] * count)
        result = type("InstallResult", (), {
            "ResultCode": self.result_code,
            "RebootRequired": self.reboot_required,
            "GetUpdateResult": lambda self, i: FakeUpdateResult(
                per_update[i]),
        })()
        return result


class FakeSession:
    def __init__(self, updates, calls=None, per_update_codes=None):
        self.updates = updates
        self.calls = calls if calls is not None else []
        self.per_update_codes = per_update_codes
        self.created = {"searcher": 0, "downloader": 0, "installer": 0,
                        "collection": 0}

    def CreateUpdateSearcher(self):
        self.created["searcher"] += 1
        return FakeSearcher(self.updates, self.calls)

    def CreateUpdateCollection(self):
        self.created["collection"] += 1
        return FakeCollection()

    def CreateUpdateDownloader(self):
        self.created["downloader"] += 1
        return FakeDownloader(self.calls)

    def CreateUpdateInstaller(self):
        self.created["installer"] += 1
        return FakeInstaller(self.calls, self.per_update_codes)


def make_updates():
    return [
        FakeUpdate("id-critical", "Critical Fix", "5034123", "Critical",
                   ("Security Updates",)),
        FakeUpdate("id-important", "Important Fix", "5034124", "Important",
                   ("Critical Updates", "Update Rollups")),
        FakeUpdate("id-moderate", "Moderate Fix", "5034125", "Moderate",
                   ("Security Updates",)),
        FakeUpdate("id-unknown", "Unrated Fix", "", ""),
    ]


class ImportTests(unittest.TestCase):
    def test_module_imports_without_pywin32(self):
        self.assertNotIn("win32com", sys.modules)
        self.assertNotIn("pythoncom", sys.modules)

    def test_detect_returns_none_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("only meaningful off Windows")
        self.assertIsNone(windows_update.detect())
        self.assertFalse(windows_update.WuaBackend.available())


class ScanTests(unittest.TestCase):
    def _scan(self):
        session = FakeSession(make_updates())
        backend = windows_update.WuaBackend(
            session_factory=lambda: session)
        return backend.scan(), session

    def test_scan_returns_plain_dicts(self):
        results, _ = self._scan()
        json.dumps(results)
        self.assertEqual(
            sorted(results[0].keys()),
            sorted(["update_id", "title", "kb", "severity", "categories",
                    "reboot_required", "is_downloaded", "is_mandatory"]))

    def test_scan_reassembles_kb_prefix(self):
        results, _ = self._scan()
        by_id = {r["update_id"]: r for r in results}
        self.assertEqual(by_id["id-critical"]["kb"], "KB5034123")
        self.assertEqual(by_id["id-critical"]["severity"], "critical")
        self.assertEqual(by_id["id-critical"]["categories"],
                         ["Security Updates"])
        self.assertEqual(by_id["id-unknown"]["kb"], "")

    def test_scan_appends_criteria_extra(self):
        session = FakeSession(make_updates())
        backend = windows_update.WuaBackend(
            session_factory=lambda: session)
        backend.scan("AutoApplyDate!=''")
        self.assertEqual(
            session.calls,
            ["IsInstalled=0 AND Type='Software' AND IsHidden=0 "
             "AND (AutoApplyDate!='')"])


class FilterTests(unittest.TestCase):
    def _dicts(self):
        return [
            {"update_id": "1", "kb": "KB5034123",
             "severity": "critical",
             "categories": ["Security Updates"]},
            {"update_id": "2", "kb": "5034124",
             "severity": "important",
             "categories": ["Update Rollups"]},
            {"update_id": "3", "kb": "5034125",
             "severity": "moderate",
             "categories": ["Security Updates", "Definition Updates"]},
            {"update_id": "4", "kb": "",
             "severity": "", "categories": []},
        ]

    def test_filter_by_classification_is_case_insensitive(self):
        kept = filter_updates(self._dicts(), classifications=["security updates"])
        self.assertEqual([u["update_id"] for u in kept], ["1", "3"])

    def test_include_kb_accepts_both_forms(self):
        kept = filter_updates(self._dicts(), include_kb=["KB5034124"])
        self.assertEqual([u["update_id"] for u in kept], ["2"])
        kept = filter_updates(self._dicts(), include_kb=["5034124", "KB5034125"])
        self.assertEqual([u["update_id"] for u in kept], ["2", "3"])

    def test_exclude_kb_wins_over_include_kb(self):
        kept = filter_updates(self._dicts(),
                              include_kb=["5034123", "KB5034124"],
                              exclude_kb=["5034124"])
        self.assertEqual([u["update_id"] for u in kept], ["1"])

    def test_severity_floor_ranks_not_string_compares(self):
        kept = filter_updates(self._dicts(), severity_floor="important")
        self.assertEqual([u["update_id"] for u in kept], ["1", "2"])
        # The naive `severity >= "important"` string comparison keeps
        # "moderate" ("m" > "i") -- the ranking dict must not.
        self.assertNotIn("3", [u["update_id"] for u in kept])

    def test_severity_floor_drops_unrated_updates(self):
        kept = filter_updates(self._dicts(), severity_floor="low")
        self.assertEqual([u["update_id"] for u in kept], ["1", "2", "3"])
        kept = filter_updates(self._dicts())
        self.assertEqual(len(kept), 4)

    def test_normalize_kb_is_case_and_prefix_insensitive(self):
        self.assertEqual(normalize_kb("kb5034123"), "5034123")
        self.assertEqual(normalize_kb("KB5034123"), "5034123")
        self.assertEqual(normalize_kb("5034123"), "5034123")
        self.assertEqual(normalize_kb(""), "")


class InstallTests(unittest.TestCase):
    def _backend(self, updates=None, per_update_codes=None):
        self.session = FakeSession(updates or make_updates(),
                                   per_update_codes=per_update_codes)
        return windows_update.WuaBackend(
            session_factory=lambda: self.session)

    def _ids(self, *update_ids):
        return list(update_ids)

    def test_install_with_empty_selection_does_not_call_install(self):
        class ExplodingInstaller(FakeInstaller):
            def Install(self):
                self.fail("Install() must not be called for an empty "
                          "selection")

        session = FakeSession(make_updates())
        session.CreateUpdateInstaller = (
            lambda: ExplodingInstaller(session.calls))
        backend = windows_update.WuaBackend(
            session_factory=lambda: session)
        result = backend.install([])
        self.assertEqual(result["result_code"],
                         windows_update.RESULT_NOT_STARTED)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["installed"], [])
        self.assertEqual(session.created["downloader"], 0)

    def test_install_reports_unknown_ids_as_failed(self):
        backend = self._backend()
        result = backend.install(["no-such-id"])
        self.assertEqual(result["failed"], ["no-such-id"])
        self.assertEqual(result["installed"], [])
        self.assertNotIn("install",
                         [call[0] for call in self.session.calls])

    def test_install_mixed_known_and_unknown(self):
        backend = self._backend()
        result = backend.install(["no-such-id", "id-critical"])
        self.assertEqual(result["installed"], ["id-critical"])
        self.assertEqual(result["failed"], ["no-such-id"])
        self.assertEqual(result["result_code"], RESULT_SUCCEEDED)

    def test_install_never_reboots(self):
        backend = self._backend()
        result = backend.install(self._ids("id-critical", "id-moderate"))
        self.assertFalse(result["reboot_required"])
        for call in self.session.calls:
            self.assertNotIn("reboot", call[0].lower())

    def test_install_accepts_eula_before_adding(self):
        updates = make_updates()
        backend = self._backend(updates)
        backend.install(["id-critical"])
        self.assertTrue(updates[0].accepted_eula)
        # EulaAccepted is checked before Add, so the collection holds the
        # same (now-accepted) update object.
        self.assertIs(self.session.updates[0], updates[0])

    def test_install_reports_per_update_failures(self):
        updates = make_updates()
        # Code 3 (SucceededWithErrors) is a successful install with errors;
        # code 4 (Failed) is not -- the routing is by per-update code, and a
        # naive "any error means failed" would misroute the first one.
        backend = self._backend(updates, per_update_codes=[
            3, windows_update.RESULT_FAILED])
        result = backend.install(["id-critical", "id-moderate"])
        self.assertEqual(result["installed"], ["id-critical"])
        self.assertEqual(result["failed"], ["id-moderate"])

    def test_install_result_shape_is_json_serializable(self):
        backend = self._backend()
        result = backend.install(["id-important"])
        json.dumps(result)
        self.assertEqual(
            sorted(result.keys()),
            sorted(["result_code", "reboot_required", "installed", "failed",
                    "detail"]))


class ExecutorHandlerTests(unittest.TestCase):
    """The wiring that turns the module into actions. The allowlist test is
    the one that catches the silent half-working state: a managed-mode agent
    rejects an action full_control accepts if the name is missing from
    _ALL_ACTIONS."""

    @staticmethod
    def _config():
        return AgentConfig(server_url="https://vigil.example.com",
                           agent_token="t", mode="full_control")

    def test_scan_handler_refuses_off_windows(self):
        with patch.object(windows_update, "detect", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                executor._windows_update_scan({}, self._config())
        self.assertIn("Windows", str(ctx.exception))

    def test_install_handler_refuses_off_windows(self):
        with patch.object(windows_update, "detect", return_value=None):
            with self.assertRaises(ValueError) as ctx:
                executor._windows_update_install({}, self._config())
        self.assertIn("Windows", str(ctx.exception))

    def test_install_handler_skips_install_when_filter_empties(self):
        class FakeBackend:
            def scan(self, criteria_extra=""):
                return [
                    {"update_id": "1", "title": "Security update",
                     "kb": "KB1", "severity": "critical",
                     "categories": ["Security Updates"],
                     "reboot_required": True, "is_downloaded": False,
                     "is_mandatory": True},
                ]

            def install(self, update_ids):
                self.installed = list(update_ids)
                return {"result_code": RESULT_SUCCEEDED,
                        "reboot_required": True, "installed": update_ids,
                        "failed": [], "detail": "succeeded"}

        backend = FakeBackend()
        with patch.object(windows_update, "detect", return_value=backend):
            out = executor._windows_update_install(
                {"include_kb": ["KB9999999"]}, self._config())
        data = json.loads(out)
        self.assertFalse(hasattr(backend, "installed"),
                         "install must not be called when the filter leaves "
                         "zero updates")
        self.assertEqual(data["installed"], [])
        self.assertEqual(data["failed"], [])
        self.assertIn("nothing installed", data["detail"])

    def test_scan_handler_filters_and_reports(self):
        updates = [
            {"update_id": "1", "title": "Critical update", "kb": "KB1",
             "severity": "critical", "categories": ["Security Updates"],
             "reboot_required": False, "is_downloaded": True,
             "is_mandatory": True},
            {"update_id": "2", "title": "Low update", "kb": "KB2",
             "severity": "low", "categories": ["Updates"],
             "reboot_required": False, "is_downloaded": False,
             "is_mandatory": False},
        ]

        class FakeBackend:
            def scan(self, criteria_extra=""):
                return list(updates)

        with patch.object(windows_update, "detect", return_value=FakeBackend()):
            out = executor._windows_update_scan(
                {"severity_floor": "important"}, self._config())
        data = json.loads(out)
        self.assertTrue(data["supported"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["updates"][0]["update_id"], "1")

    def test_install_handler_reports_reboot_without_rebooting(self):
        class FakeBackend:
            def scan(self, criteria_extra=""):
                return [
                    {"update_id": "1", "title": "Update", "kb": "KB1",
                     "severity": "critical", "categories": [],
                     "reboot_required": True, "is_downloaded": True,
                     "is_mandatory": True},
                ]

            def install(self, update_ids):
                return {"result_code": RESULT_SUCCEEDED,
                        "reboot_required": True, "installed": list(update_ids),
                        "failed": [], "detail": "succeeded"}

        backend = FakeBackend()
        with patch.object(windows_update, "detect", return_value=backend):
            out = executor._windows_update_install({}, self._config())
        data = json.loads(out)
        self.assertTrue(data["reboot_required"])
        self.assertEqual(data["installed"], ["1"])
        json.dumps(data)

    def test_handlers_registered(self):
        self.assertIn("windows_update_scan", executor._HANDLERS)
        self.assertIn("windows_update_install", executor._HANDLERS)

    def test_actions_are_allowlistable(self):
        self.assertIn("windows_update_scan", _ALL_ACTIONS)
        self.assertIn("windows_update_install", _ALL_ACTIONS)


if __name__ == "__main__":
    unittest.main()
