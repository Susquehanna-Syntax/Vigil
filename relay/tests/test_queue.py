"""Offline buffer semantics: FIFO order, both limits, atomic writes."""

import os
import tempfile
import unittest

from vigil_relay.queue import DiskQueue


class DiskQueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dirpath = os.path.join(self._tmp.name, "queue")
        self.addCleanup(self._tmp.cleanup)

    def queue(self, max_bytes=50 * 1024 * 1024, max_items=10_000):
        return DiskQueue(self.dirpath, max_bytes, max_items)

    def test_put_items_load_drop_in_order(self):
        q = self.queue()
        self.assertEqual(q.items(), [])
        for n in range(3):
            self.assertTrue(q.put({"t": "req", "n": n}))
        paths = q.items()
        self.assertEqual(len(paths), 3)
        self.assertEqual([q.load(p)["n"] for p in paths], [0, 1, 2])
        self.assertTrue(os.path.isdir(self.dirpath))
        self.assertEqual(oct(os.stat(self.dirpath).st_mode & 0o777), "0o700")

        q.drop(paths[0])
        self.assertEqual([q.load(p)["n"] for p in q.items()], [1, 2])
        q.drop(paths[0])  # dropping twice is not an error

    def test_trims_to_max_items_keeping_newest(self):
        q = self.queue(max_items=3)
        for n in range(6):
            q.put({"t": "req", "n": n})
        self.assertEqual([q.load(p)["n"] for p in q.items()], [3, 4, 5])

    def test_trims_to_max_bytes_keeping_newest(self):
        probe = self.queue()
        probe.put({"t": "req", "body": "x" * 128})
        one = os.path.getsize(probe.items()[0])
        q = self.queue(max_bytes=one * 2 + one // 2, max_items=100)
        for n in range(5):
            q.put({"t": "req", "body": "x" * 128})
        remaining = q.items()
        self.assertEqual(len(remaining), 2)
        self.assertTrue(sum(os.path.getsize(p) for p in remaining) <= one * 2 + one // 2)

    def test_refuses_a_frame_larger_than_max_bytes(self):
        q = self.queue(max_bytes=64)
        self.assertFalse(q.put({"t": "req", "body": "y" * 4096}))
        self.assertEqual(q.items(), [])

    def test_no_temp_files_left_behind(self):
        q = self.queue()
        q.put({"t": "req"})
        self.assertEqual([n for n in os.listdir(self.dirpath) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
