"""hunt_registry and hunt_content: the last two hunts (M5 phase 05).

Registry tests inject a fake ``winreg`` module over a nested dict and patch
``sys.platform`` to win32; content tests build files in a temp tree and pass
``paths=<tmp>`` so the suite stays hermetic.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, hunt
from vigil_agent.config import AgentConfig


def _config(**kw):
    base = {"server_url": "https://vigil.example.com", "agent_token": "t",
            "mode": "full_control", "data_dir": Path("/nonexistent-vigil-data")}
    base.update(kw)
    return AgentConfig(**base)


class FakeWinreg:
    """A winreg stand-in over a nested dict.

    ``tree`` maps subkey path (relative to a root, e.g.
    ``"Uninstall\\Sub"`` or ``""`` for the root key) to a dict of values.
    ``values`` maps ``root|path|name`` -> ``(data, vtype)``.
    """

    class _Error(Exception):
        pass

    def __init__(self, tree: dict, values: dict):
        self._tree = tree
        self._values = values
        self._open_keys = {}
        self._next_handle = 0
        self.opened_with = []

    # ── module surface ───────────────────────────────────────────────
    KEY_READ = 2
    KEY_WOW64_64KEY = 0x100
    KEY_WOW64_32KEY = 0x2
    HKEY_LOCAL_MACHINE = 0
    HKEY_CURRENT_USER = 1
    HKEY_USERS = 2

    def OpenKey(self, hkey, path, reserved=0, access=0):
        norm = path.rstrip("\\")
        if norm not in self._tree:
            raise OSError(f"key missing: {hkey}\\{path}")
        self._next_handle += 1
        handle = (hkey, norm)
        self._open_keys[handle] = True
        self.opened_with.append((hkey, norm, access))
        return handle

    def CloseKey(self, handle):
        self._open_keys.pop(handle, None)

    def EnumKey(self, handle, index):
        _, path = handle
        subs = self._direct_children(path)
        if index >= len(subs):
            raise OSError("no more subkeys")
        return subs[index]

    def EnumValue(self, handle, index):
        _, path = handle
        names = self._value_names(path)
        if index >= len(names):
            raise OSError("no more values")
        # Real winreg.EnumValue returns (name, data, type), not just the name.
        name = names[index]
        data, vtype = self._values[(path, name)]
        return name, data, vtype

    def QueryValueEx(self, handle, name):
        _, path = handle
        if (path, name) not in self._values:
            raise OSError(f"value missing: {name}")
        return self._values[(path, name)]

    # ── helpers for tests ────────────────────────────────────────────
    def _direct_children(self, path):
        prefix = path + "\\" if path else ""
        seen = set()
        for key in self._tree:
            if key.startswith(prefix):
                rest = key[len(prefix):]
                if rest:
                    seen.add(rest.split("\\", 1)[0])
        return sorted(seen)

    def _value_names(self, path):
        return sorted(name for (p, name) in self._values if p == path)


def _fake_winreg_module(fake: FakeWinreg):
    import types
    mod = types.ModuleType("winreg")
    for attr in ("KEY_READ", "KEY_WOW64_64KEY", "KEY_WOW64_32KEY",
                 "HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER", "HKEY_USERS"):
        setattr(mod, attr, getattr(fake, attr))
    mod.OpenKey = lambda hkey, path, *a, **kw: fake.OpenKey(hkey, path, *a, **kw)
    mod.CloseKey = fake.CloseKey
    mod.EnumKey = fake.EnumKey
    mod.EnumValue = fake.EnumValue
    mod.QueryValueEx = fake.QueryValueEx
    return mod


class HuntRegistryProbeTests(unittest.TestCase):
    UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

    def _fake(self):
        u = self.UNINSTALL
        tree = {
            u: None,
            u + "\\{Java-17}": None,
            u + "\\{Other-App}": None,
            u + "\\{Java-17}\\Sub": None,
        }
        values = {
            (u + "\\{Java-17}", "DisplayName"): ("Java (TM) SE Development Kit 17.0.2", 1),
            (u + "\\{Java-17}", "UninstallString"): ("C:\\Program Files\\Java\\uninstall.exe", 1),
            (u + "\\{Other-App}", "DisplayName"): ("Some Other App", 1),
        }
        return FakeWinreg(tree, values)

    def test_registry_windows_only(self):
        # run_hunt wraps probe errors in RuntimeError; the raw probe raises
        # ValueError, which is what the executor's error path sees.
        result = hunt.HuntResult(10, 60)
        with self.assertRaises(ValueError) as ctx:
            hunt.hunt_registry(result, {"key": "HKLM\\SOFTWARE"})
        self.assertIn("Windows only", str(ctx.exception))
        with self.assertRaises(RuntimeError):
            hunt.run_hunt(hunt.hunt_registry, {"key": "HKLM\\SOFTWARE"})

    def test_registry_key_without_value_filter_reports_keys(self):
        fake = self._fake()
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            out = hunt.run_hunt(hunt.hunt_registry, {"key": f"HKLM\\{self.UNINSTALL}"})
        body = json.loads(out)
        keys = {m["key"] for m in body["matches"]}
        # The key itself (value=None) plus nothing else — no subkeys here.
        self.assertIn(f"HKLM\\{self.UNINSTALL}", keys)
        base = f"HKLM\\{self.UNINSTALL}"
        base_match = next(m for m in body["matches"] if m["key"] == base)
        self.assertIsNone(base_match["value"])
        self.assertIsNone(base_match["data"])
        self.assertEqual(base_match["type"], "REG_KEY")

    def test_registry_subkey_wildcard_and_filters(self):
        fake = self._fake()
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            out = hunt.run_hunt(hunt.hunt_registry, {
                "key": f"HKLM\\{self.UNINSTALL}\\*",
                "value": "DisplayName",
                "data": "java",
            })
        body = json.loads(out)
        self.assertEqual(len(body["matches"]), 1)
        match = body["matches"][0]
        self.assertEqual(match["key"],
                         f"HKLM\\{self.UNINSTALL}\\{{Java-17}}")
        self.assertEqual(match["value"], "DisplayName")
        self.assertEqual(match["data"], "Java (TM) SE Development Kit 17.0.2")
        self.assertEqual(match["type"], "REG_SZ")
        # data is case-insensitive: "java" matched "Java".
        # The 64-bit view flag reached OpenKey.
        _, _, access = fake.opened_with[0]
        self.assertTrue(access & 0x100)  # KEY_WOW64_64KEY by default

    def test_registry_view_32(self):
        fake = self._fake()
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            hunt.run_hunt(hunt.hunt_registry, {
                "key": f"HKLM\\{self.UNINSTALL}", "view": "32"})
        # 32-bit view: KEY_WOW64_32KEY set, 64-bit flag cleared.
        self.assertTrue(any(a & 0x2 and not a & 0x100
                            for (_, _, a) in fake.opened_with))

    def test_registry_data_filter_excludes_non_matching(self):
        fake = self._fake()
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            out = hunt.run_hunt(hunt.hunt_registry, {
                "key": f"HKLM\\{self.UNINSTALL}\\*",
                "value": "DisplayName",
                "data": "nope-no-such-string",
            })
        body = json.loads(out)
        self.assertEqual(body["matches"], [])
        self.assertEqual(out.data["matched"], False)

    def test_registry_data_cut_to_500(self):
        fake = FakeWinreg({r"Root": None},
                          {(r"Root", "Long"): ("x" * 1234, 1)})
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            out = hunt.run_hunt(hunt.hunt_registry, {
                "key": "HKLM\\Root", "value": "Long"})
        body = json.loads(out)
        self.assertEqual(len(body["matches"][0]["data"]), 500)

    def test_registry_bad_root_refused(self):
        fake = self._fake()
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}), \
                self.assertRaises(RuntimeError) as ctx:
            hunt.run_hunt(hunt.hunt_registry, {"key": "HKEY_BOGUS\\X"})
        self.assertIn("HKLM", str(ctx.exception))


    def test_registry_wildcard_lists_subkeys_not_the_key_itself(self):
        fake = self._fake()
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            out = hunt.run_hunt(hunt.hunt_registry, {
                "key": f"HKLM\\{self.UNINSTALL}\\*"})
        keys = sorted(m["key"] for m in json.loads(out)["matches"])
        base = f"HKLM\\{self.UNINSTALL}"
        self.assertEqual(keys, [base + "\\{Java-17}", base + "\\{Other-App}"])

    def test_registry_data_filter_without_value_filter(self):
        # data alone searches every value, rather than being ignored.
        fake = self._fake()
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            out = hunt.run_hunt(hunt.hunt_registry, {
                "key": f"HKLM\\{self.UNINSTALL}\\*", "data": "uninstall.exe"})
        matches = json.loads(out)["matches"]
        self.assertEqual([(m["value"], m["type"]) for m in matches],
                         [("UninstallString", "REG_SZ")])

    def test_registry_binary_data_is_hex(self):
        fake = FakeWinreg({r"Root": None}, {(r"Root", "Blob"): (b"\x00\xffA", 3)})
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": _fake_winreg_module(fake)}):
            out = hunt.run_hunt(hunt.hunt_registry, {"key": "HKLM\\Root", "value": "*"})
        match = json.loads(out)["matches"][0]
        self.assertEqual((match["data"], match["type"]), ("00ff41", "REG_BINARY"))


class HuntContentProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.params = {"paths": str(self.root)}

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data)
        return path

    def _run(self, **params):
        merged = dict(self.params)
        merged.update(params)
        merged["return"] = merged.get("ret", "lines")
        merged.pop("ret", None)
        out = hunt.run_hunt(hunt.hunt_content, merged)
        return json.loads(out), out

    def test_content_lines_default(self):
        p = self._write("app.conf",
                        "line one\nAKIAABCDEFGHIJKLMNOP is here\nplain\n"
                        "second AKIA0123456789ABCDEF hit\n")
        body, out = self._run(pattern="AKIA[A-Z0-9]{16}")
        self.assertEqual(len(body["matches"]), 1)
        match = body["matches"][0]
        self.assertEqual(match["path"], str(p))
        self.assertEqual(match["match_count"], 2)
        self.assertEqual(match["lines"], "2,4")
        self.assertIsNone(match["text"])
        self.assertEqual(out.data, {"matched": True, "count": 1,
                                    "truncated": False})

    def test_content_line_with_two_hits_is_listed_once(self):
        self._write("dup.conf", "AKIAABCDEFGHIJKLMNOP AKIA0123456789ABCDEF\n")
        body, _ = self._run(pattern="AKIA[A-Z0-9]{16}")
        match = body["matches"][0]
        self.assertEqual((match["match_count"], match["lines"]), (2, "1"))

    def test_content_match_only(self):
        self._write("app.conf", "a AKIAABCDEFGHIJKLMNOP b\n")
        body, _ = self._run(pattern="AKIA[A-Z]{16}", ret="match")
        match = body["matches"][0]
        self.assertIsNone(match["lines"])
        self.assertIsNone(match["text"])
        self.assertEqual(match["match_count"], 1)

    def test_content_text_is_capped(self):
        # 25 distinct matches of a long token: only the first 20 survive,
        # each cut to 200 chars, joined with " | ".
        lines = [f"token {i} AKIA{'A' * 300}{i}" for i in range(25)]
        self._write("big.conf", "\n".join(lines) + "\n")
        body, _ = self._run(pattern=r"AKIA[A-Za-z0-9]{30,}", ret="text")
        match = body["matches"][0]
        self.assertEqual(match["match_count"], 25)
        parts = match["text"].split(" | ")
        self.assertEqual(len(parts), 20)
        self.assertTrue(all(len(part) <= 200 for part in parts))
        # lines are reported too when return is text.
        self.assertEqual(match["lines"], ",".join(str(i) for i in range(1, 26)))

    def test_content_lines_capped_at_50(self):
        lines = [f"AKIAABCDEFGHIJKLMNOP{i}" for i in range(80)]
        self._write("many.conf", "\n".join(lines) + "\n")
        body, _ = self._run(pattern="AKIA[A-Z0-9]{16}")
        match = body["matches"][0]
        self.assertEqual(match["match_count"], 80)
        self.assertEqual(match["lines"], ",".join(str(i) for i in range(1, 51)))

    def test_content_skips_binaries_and_big_files(self):
        self._write("bin.dat", b"AKIA\x00\x01\x02binary" * 10)
        self._write("big.conf", "AKIAABCDEFGHIJKLMNOP" + "z" * 2048, )
        small = self._write("small.conf", "AKIAABCDEFGHIJKLMNOP\n")
        body, _ = self._run(pattern="AKIA[A-Z]{16}",
                            max_file_size=1024)
        paths = [m["path"] for m in body["matches"]]
        self.assertEqual(paths, [str(small)])

    def test_content_name_glob(self):
        self._write("a.conf", "AKIAABCDEFGHIJKLMNOP\n")
        self._write("b.txt", "AKIAABCDEFGHIJKLMNOP\n")
        body, _ = self._run(pattern="AKIA[A-Z]{16}", name="*.conf")
        self.assertEqual([m["path"] for m in body["matches"]],
                         [str(self.root / "a.conf")])

    def test_content_utf8_replacement(self):
        self._write("utf.conf",
                    b"AKIAABCDEFGHIJKLMNOP\xff\xfe broken "
                    b"AKIA0123456789ABCDEF\n")
        body, _ = self._run(pattern="AKIA[A-Z0-9]{16}")
        match = body["matches"][0]
        self.assertEqual(match["match_count"], 2)

    def test_content_line_scan_limit(self):
        # The match sits past the 4,096th character of the line: not found.
        long_line = "x" * 5000 + "AKIAABCDEFGHIJKLMNOP"
        self._write("long.conf", long_line + "\n")
        body, _ = self._run(pattern="AKIA[A-Z]{16}")
        self.assertEqual(body["matches"], [])

    def test_content_bad_pattern(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(pattern="([unclosed")
        self.assertIn("pattern", str(ctx.exception))

    def test_content_bad_return(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(pattern="a", ret="everything")
        self.assertIn("return", str(ctx.exception))

    def test_content_no_match(self):
        self._write("a.conf", "nothing here\n")
        body, out = self._run(pattern="AKIA[A-Z]{16}")
        self.assertEqual(body["matches"], [])
        self.assertEqual(out.data, {"matched": False, "count": 0,
                                    "truncated": False})


class HuntContentActionTests(unittest.TestCase):
    def test_through_execute_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "leak.conf").write_text(
                "key=AKIAABCDEFGHIJKLMNOP\n")
            out = executor.execute_action(
                "hunt_content",
                {"pattern": "AKIA[A-Z]{16}", "paths": tmp},
                _config())
            body = json.loads(out)
            self.assertIn("matches", body)
            self.assertIn("duration", body)
            self.assertTrue(out.data["matched"])
            self.assertEqual(out.data["count"], 1)
            self.assertEqual(out.data["truncated"], False)


if __name__ == "__main__":
    unittest.main()
