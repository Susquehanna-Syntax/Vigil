"""`vigil-agent measure` (sprint M8): the numbers every later agent change is
judged by, so the measuring itself has to be right."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil
from vigil_agent import measure

AGENT_DIR = Path(__file__).resolve().parents[1]


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(measure, "_checkin_collection", lambda: sum(range(200_000))):
            cls.report = measure.measure(software=False, passes=3)

    def test_the_report_has_every_number_and_they_are_plausible(self):
        r = self.report
        self.assertFalse(r["frozen"])
        self.assertIsNone(r["installed_bytes"], "from source there is no install to weigh")
        self.assertGreater(r["idle_rss_bytes"], 5 * 1024 * 1024)
        self.assertLess(r["idle_rss_bytes"], 2 * 1024 ** 3)
        self.assertGreaterEqual(r["startup_seconds"], 0)
        for key in ("checkin_cold", "checkin_warm"):
            self.assertGreaterEqual(r[key]["wall_seconds"], 0)
            self.assertGreaterEqual(r[key]["cpu_seconds"], 0)
        self.assertEqual(r["checkin_warm"]["passes"], 3)
        self.assertNotIn("software_scan", r)
        json.dumps(r)

    def test_the_row_has_one_cell_per_column_of_the_docs_table(self):
        row = measure.markdown_row(self.report)
        self.assertEqual(row.count("|"), 10)
        self.assertIn("| source |", row)
        docs = (AGENT_DIR.parent / "docs" / "agent-footprint.md").read_text()
        header = next(line for line in docs.splitlines() if line.startswith("| Platform"))
        self.assertEqual(header.count("|"), row.count("|"))

    def test_the_text_report_names_every_measure(self):
        text = measure.render({**self.report, "software_scan": {
            "wall_seconds": 2.5, "cpu_seconds": 1.0, "peak_rss_bytes": 80 * 1048576}})
        for label in ("Installed size", "Start-up", "Memory at rest", "First check-in",
                      "Each check-in after", "Software scan", "80.0 MB"):
            self.assertIn(label, text)


class MeasuringTests(unittest.TestCase):
    def test_cpu_time_counts_work_done_and_not_time_slept(self):
        process = psutil.Process()

        def spin():
            end = process.cpu_times().user + 0.2
            while process.cpu_times().user < end:
                sum(range(10_000))

        import time
        busy = measure._timed(process, spin)
        idle = measure._timed(process, lambda: time.sleep(0.2))
        self.assertGreaterEqual(busy["cpu_seconds"], 0.15)
        self.assertLess(idle["cpu_seconds"], 0.1)
        self.assertGreaterEqual(idle["wall_seconds"], 0.2)

    def test_a_collector_that_fails_is_recorded_not_raised(self):
        def boom():
            raise RuntimeError("no docker here")

        result = measure._timed(psutil.Process(), boom)
        self.assertEqual(result["error"], "RuntimeError: no docker here")

    def test_the_peak_sees_memory_that_was_freed_before_the_block_ended(self):
        process = psutil.Process()
        before = measure._rss(process)
        with measure._Peak(process) as peak:
            block = bytearray(80 * 1024 * 1024)
            block[::4096] = b"x" * len(block[::4096])  # touch every page
            import time
            time.sleep(0.15)
            del block
        self.assertGreater(peak.bytes - before, 60 * 1024 * 1024)

    def test_a_child_process_is_counted(self):
        process = psutil.Process()
        with measure._Peak(process) as peak:
            subprocess.run([sys.executable, "-c",
                            "b = bytearray(60*1024*1024); b[::4096] = b'x'*len(b[::4096]);"
                            "import time; time.sleep(0.3)"], check=True)
        self.assertGreater(peak.bytes - measure._rss(process), 40 * 1024 * 1024)


class InstalledSizeTests(unittest.TestCase):
    def test_one_file_and_one_folder_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "vigil-agent"
            exe.write_bytes(b"x" * 1000)
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "executable", str(exe)):
                self.assertEqual(measure.installed_bytes(), 1000)
                (Path(tmp) / "_internal").mkdir()
                (Path(tmp) / "_internal" / "lib.so").write_bytes(b"y" * 500)
                self.assertEqual(measure.installed_bytes(), 1500)


class CommandTests(unittest.TestCase):
    def test_measure_runs_with_no_config_and_no_network(self):
        env = {**os.environ, "VIGIL_CONFIG_PATH": "/nonexistent/agent.yml",
               # Any connection attempt would fail loudly rather than hang.
               "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}
        done = subprocess.run(
            [sys.executable, "vigil_agent_main.py", "measure", "--json", "--no-software"],
            cwd=AGENT_DIR, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertGreater(report["idle_rss_bytes"], 0)
        self.assertGreater(report["checkin_warm"]["wall_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
