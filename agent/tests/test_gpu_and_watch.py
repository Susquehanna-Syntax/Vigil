"""GPU telemetry from nvidia-smi/rocm-smi, and the process watch list.

Neither SMI tool is installed on most hosts, and a host with no GPU is the
ordinary case rather than a failure — so absence has to be silent and empty,
not an exception or a log line every scrape.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import collector
from vigil_agent.config import AgentConfig

# Real nvidia-smi output: an RTX 4070 with ECC unsupported, which is how a
# consumer card answers the ECC fields.
_NVIDIA_CORE = "0, NVIDIA GeForce RTX 4070, 43, 1142, 12282, 39, 35.29\n"
_NVIDIA_WIDE = (
    "0, NVIDIA GeForce RTX 4070, 43, 1142, 12282, 39, 35.29, 32, 1560, 5001, "
    "13, 4, 16, [N/A], [N/A]\n"
)


def _proc(stdout="", returncode=0, stderr=""):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def _by_metric(points):
    return {p["metric"]: p["value"] for p in points}


class NvidiaTests(unittest.TestCase):
    def test_core_fields_become_points(self):
        with patch.object(collector.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             patch.object(collector.subprocess, "run", return_value=_proc(_NVIDIA_CORE)):
            points = collector.collect_nvidia_gpu()
        values = _by_metric(points)
        self.assertEqual(values["utilization_percent"], 43.0)
        self.assertEqual(values["memory_used_mb"], 1142.0)
        self.assertEqual(values["temperature_celsius"], 39.0)
        self.assertEqual(values["power_watts"], 35.29)
        # Derived rather than left for the dashboard to divide two series.
        self.assertAlmostEqual(values["memory_percent"], 9.3, places=1)
        for point in points:
            self.assertEqual(point["labels"]["vendor"], "nvidia")
            self.assertEqual(point["labels"]["index"], "0")
            self.assertEqual(point["labels"]["name"], "NVIDIA GeForce RTX 4070")

    def test_extended_adds_the_wide_set_and_drops_unsupported_fields(self):
        with patch.object(collector.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             patch.object(collector.subprocess, "run", return_value=_proc(_NVIDIA_WIDE)):
            values = _by_metric(collector.collect_nvidia_gpu(extended=True))
        self.assertEqual(values["fan_percent"], 32.0)
        self.assertEqual(values["clock_graphics_mhz"], 1560.0)
        self.assertEqual(values["pcie_width"], 16.0)
        # "[N/A]" is how nvidia-smi says a field does not apply to this card.
        # It must not become a zero, which would read as "no ECC errors".
        self.assertNotIn("ecc_corrected", values)
        self.assertNotIn("ecc_uncorrected", values)

    def test_no_gpu_is_silent_and_empty(self):
        with patch.object(collector.shutil, "which", return_value=None):
            self.assertEqual(collector.collect_nvidia_gpu(), [])

    def test_a_short_row_is_skipped_rather_than_misaligned(self):
        # A truncated row would otherwise zip values onto the wrong metrics.
        with patch.object(collector.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             patch.object(collector.subprocess, "run", return_value=_proc("0, GPU, 43\n")):
            self.assertEqual(collector.collect_nvidia_gpu(), [])

    def test_a_failing_tool_yields_nothing(self):
        with patch.object(collector.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             patch.object(collector.subprocess, "run",
                          return_value=_proc(returncode=9, stderr="driver mismatch")):
            self.assertEqual(collector.collect_nvidia_gpu(), [])


class RocmTests(unittest.TestCase):
    payload = (
        '{"card0": {"Card series": "Radeon RX 7900 XTX",'
        ' "GPU use (%)": "62", "VRAM Total Memory (B)": "25753026560",'
        ' "VRAM Total Used Memory (B)": "4294967296",'
        ' "Temperature (Sensor edge) (C)": "51.0",'
        ' "Average Graphics Package Power (W)": "118.0",'
        ' "Fan speed (%)": "44"}}'
    )

    def test_values_are_parsed_and_bytes_become_megabytes(self):
        with patch.object(collector.shutil, "which", return_value="/usr/bin/rocm-smi"), \
             patch.object(collector.subprocess, "run", return_value=_proc(self.payload)):
            points = collector.collect_amd_gpu()
        values = _by_metric(points)
        self.assertEqual(values["utilization_percent"], 62.0)
        self.assertEqual(values["temperature_celsius"], 51.0)
        self.assertEqual(values["power_watts"], 118.0)
        self.assertAlmostEqual(values["memory_used_mb"], 4096.0, places=0)
        self.assertAlmostEqual(values["memory_percent"], 16.7, places=1)
        self.assertEqual(points[0]["labels"]["vendor"], "amd")
        self.assertEqual(points[0]["labels"]["name"], "Radeon RX 7900 XTX")

    def test_the_wide_set_is_opt_in(self):
        with patch.object(collector.shutil, "which", return_value="/usr/bin/rocm-smi"), \
             patch.object(collector.subprocess, "run", return_value=_proc(self.payload)):
            core = _by_metric(collector.collect_amd_gpu())
            wide = _by_metric(collector.collect_amd_gpu(extended=True))
        self.assertNotIn("fan_percent", core)
        self.assertEqual(wide["fan_percent"], 44.0)

    def test_non_json_output_yields_nothing(self):
        with patch.object(collector.shutil, "which", return_value="/usr/bin/rocm-smi"), \
             patch.object(collector.subprocess, "run", return_value=_proc("not json")):
            self.assertEqual(collector.collect_amd_gpu(), [])


class ProcessWatchTests(unittest.TestCase):
    def _procs(self):
        return [
            {"pid": 10, "name": "postgres", "cpu_percent": 4.0, "memory_percent": 1.5},
            {"pid": 11, "name": "postgres", "cpu_percent": 2.0, "memory_percent": 0.5},
            {"pid": 12, "name": "chrome", "cpu_percent": 90.0, "memory_percent": 20.0},
        ]

    def _collect(self, watch, procs=None):
        entries = []
        for info in (procs if procs is not None else self._procs()):
            handle = MagicMock()
            handle.info = info
            entries.append(handle)
        with patch.object(collector.psutil, "process_iter", return_value=entries):
            return collector.collect_top_processes(n=1, watch=watch)

    def test_a_watched_process_is_reported_even_when_it_does_not_rank(self):
        # n=1, so only chrome ranks; postgres appears solely because it is watched.
        watched = [p for p in self._collect(["postgres"])
                   if p["labels"].get("watched") == "1"]
        self.assertEqual({p["metric"] for p in watched},
                         {"cpu_percent", "memory_percent", "instances"})
        self.assertTrue(all(p["labels"]["name"] == "postgres" for p in watched))

    def test_processes_sharing_a_name_are_summed_and_counted(self):
        watched = {p["metric"]: p["value"] for p in self._collect(["postgres"])
                   if p["labels"].get("watched") == "1"}
        self.assertEqual(watched["cpu_percent"], 6.0)     # 4.0 + 2.0
        self.assertEqual(watched["memory_percent"], 2.0)  # 1.5 + 0.5
        self.assertEqual(watched["instances"], 2)

    def test_a_watched_process_that_is_not_running_reports_zero(self):
        # Silence would be indistinguishable from the agent being down; zero is
        # how the chart shows the outage.
        watched = {p["metric"]: p["value"] for p in self._collect(["nginx"])
                   if p["labels"].get("watched") == "1"}
        self.assertEqual(watched["instances"], 0)
        self.assertEqual(watched["cpu_percent"], 0)

    def test_ranked_rows_are_not_marked_watched(self):
        ranked = [p for p in self._collect(["postgres"])
                  if "rank" in p["labels"]]
        self.assertTrue(ranked)
        self.assertTrue(all("watched" not in p["labels"] for p in ranked))


class ConfigTests(unittest.TestCase):
    def _config(self, **kwargs):
        return AgentConfig(server_url="https://vigil.example", agent_token="t", **kwargs)

    def test_watch_list_is_trimmed_and_deduped_but_keeps_case(self):
        config = self._config(process_watch=["  postgres ", "postgres", "", "Xorg"])
        self.assertEqual(config.process_watch, ["postgres", "Xorg"])

    def test_an_over_long_name_is_refused(self):
        with self.assertRaises(ValueError):
            self._config(process_watch=["p" * 121])

    def test_defaults_watch_nothing_and_keep_gpu_narrow(self):
        config = self._config()
        self.assertEqual(config.process_watch, [])
        self.assertFalse(config.gpu_extended)


class CollectAllTests(unittest.TestCase):
    def test_config_drives_the_watch_list_and_the_gpu_width(self):
        config = AgentConfig(server_url="https://vigil.example", agent_token="t",
                             process_watch=["postgres"], gpu_extended=True)
        with patch.object(collector, "collect_cpu", return_value=[]), \
             patch.object(collector, "collect_memory", return_value=[]), \
             patch.object(collector, "collect_disk", return_value=[]), \
             patch.object(collector, "collect_network", return_value=[]), \
             patch.object(collector, "collect_temperatures", return_value=[]), \
             patch.object(collector, "collect_top_processes", return_value=[]) as procs, \
             patch.object(collector, "collect_gpu", return_value=[]) as gpu:
            collector.collect_all(config)
        procs.assert_called_once_with(watch=["postgres"])
        gpu.assert_called_once_with(True)

    def test_no_config_watches_nothing(self):
        with patch.object(collector, "collect_cpu", return_value=[]), \
             patch.object(collector, "collect_memory", return_value=[]), \
             patch.object(collector, "collect_disk", return_value=[]), \
             patch.object(collector, "collect_network", return_value=[]), \
             patch.object(collector, "collect_temperatures", return_value=[]), \
             patch.object(collector, "collect_top_processes", return_value=[]) as procs, \
             patch.object(collector, "collect_gpu", return_value=[]) as gpu:
            collector.collect_all()
        procs.assert_called_once_with(watch=[])
        gpu.assert_called_once_with(False)

    def test_one_broken_collector_does_not_lose_the_others(self):
        with patch.object(collector, "collect_cpu", return_value=[{"category": "cpu"}]), \
             patch.object(collector, "collect_memory", side_effect=RuntimeError("boom")), \
             patch.object(collector, "collect_disk", return_value=[]), \
             patch.object(collector, "collect_network", return_value=[]), \
             patch.object(collector, "collect_temperatures", return_value=[]), \
             patch.object(collector, "collect_top_processes", return_value=[]), \
             patch.object(collector, "collect_gpu", return_value=[{"category": "gpu"}]):
            points = collector.collect_all()
        self.assertEqual([p["category"] for p in points], ["cpu", "gpu"])


if __name__ == "__main__":
    unittest.main()
