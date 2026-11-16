"""CPU usage is the average since the previous check-in, not a one-second
sample that the check-in waits for (found by `vigil-agent measure`, sprint M8)."""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent.collector import basic


def _total(points):
    return next(p["value"] for p in points
                if p["metric"] == "usage_percent" and p["labels"].get("core") == "total")


class CpuWindowTests(unittest.TestCase):
    def setUp(self):
        basic._cpu_baseline = None

    def test_the_first_reading_samples_and_its_total_is_the_mean_of_the_cores(self):
        calls = []

        def fake(interval=None, percpu=False):
            calls.append((interval, percpu))
            return [10.0, 30.0] if percpu else 0.0  # psutil's first total is 0.0

        with mock.patch.object(basic.psutil, "cpu_percent", fake):
            points = basic.collect_cpu()
        self.assertEqual(calls[0], (1, True), "nothing to compare against: one blocking sample")
        self.assertEqual(_total(points), 20.0, "not the 0.0 psutil gives a first call")

    def test_later_readings_do_not_wait(self):
        basic.collect_cpu()
        time.sleep(1.1)
        with mock.patch.object(basic.psutil, "cpu_percent",
                               wraps=basic.psutil.cpu_percent) as spy:
            started = time.monotonic()
            points = basic.collect_cpu()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.3)
        for call in spy.call_args_list:
            self.assertIsNone(call.kwargs.get("interval"))
        cores = [p for p in points if p["metric"] == "usage_percent"]
        self.assertGreaterEqual(len(cores), 2)
        for p in cores:
            self.assertTrue(0.0 <= p["value"] <= 100.0, p)

    def test_the_average_covers_work_done_between_readings(self):
        basic.collect_cpu()
        end = time.process_time() + 1.2
        while time.process_time() < end:  # one core, flat out, between the two readings
            sum(range(20_000))
        points = basic.collect_cpu()
        busiest = max(p["value"] for p in points
                      if p["metric"] == "usage_percent" and p["labels"].get("core") != "total")
        self.assertGreater(busiest, 20.0, "a burst between check-ins is in the number")

    def test_two_readings_too_close_together_sample_again(self):
        basic.collect_cpu()
        with mock.patch.object(basic.psutil, "cpu_percent",
                               wraps=basic.psutil.cpu_percent) as spy:
            basic.collect_cpu()
        self.assertEqual(spy.call_args_list[0].kwargs.get("interval"), 1)

    def test_a_baseline_from_another_thread_is_not_trusted(self):
        thread = threading.Thread(target=basic.collect_cpu)
        thread.start()
        thread.join()
        time.sleep(1.1)
        with mock.patch.object(basic.psutil, "cpu_percent",
                               wraps=basic.psutil.cpu_percent) as spy:
            basic.collect_cpu()
        self.assertEqual(spy.call_args_list[0].kwargs.get("interval"), 1)


if __name__ == "__main__":
    unittest.main()
