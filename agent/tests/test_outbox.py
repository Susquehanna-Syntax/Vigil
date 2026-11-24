"""Results the server could not take are held and sent again (rc2).

The update on the dev PC failed its download with a relay 503, and the
failure report hit the same 503 and was dropped: the server showed the task
DISPATCHED for an hour. These pin the replacement behaviour."""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from vigil_agent import outbox


def http_error(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code}", response=resp)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        outbox.configure(self.dir.name)

    def tearDown(self):
        outbox._dir = None

    def test_which_failures_are_worth_another_try(self):
        self.assertTrue(outbox.worth_retrying(requests.ConnectionError()))
        self.assertTrue(outbox.worth_retrying(requests.ReadTimeout()))
        for code in (500, 502, 503, 530, 408, 429):
            self.assertTrue(outbox.worth_retrying(http_error(code)), code)
        for code in (400, 401, 404, 409):
            self.assertFalse(outbox.worth_retrying(http_error(code)), code)
        self.assertFalse(outbox.worth_retrying(ValueError()))

    def test_a_held_result_is_delivered_by_the_next_flush(self):
        outbox.keep("t1", "failed", "503 on download")
        sent = []
        self.assertEqual(outbox.flush(lambda *a: sent.append(a)), 1)
        self.assertEqual(sent, [("t1", "failed", "503 on download", None)])
        self.assertEqual(outbox.pending(), 0)

    def test_a_server_still_down_keeps_everything_in_order(self):
        outbox.keep("t1", "failed", "a")
        outbox.keep("t2", "completed", "b")

        def down(*_):
            raise http_error(503)

        self.assertEqual(outbox.flush(down), 0)
        self.assertEqual(outbox.pending(), 2)
        sent = []
        outbox.flush(lambda *a: sent.append(a[0]))
        self.assertEqual(sent, ["t1", "t2"])

    def test_a_refused_result_is_dropped_and_the_rest_still_go(self):
        outbox.keep("gone", "failed", "a")
        outbox.keep("t2", "completed", "b")
        sent = []

        def send(task_id, *_):
            if task_id == "gone":
                raise http_error(404)
            sent.append(task_id)

        self.assertEqual(outbox.flush(send), 1)
        self.assertEqual(sent, ["t2"])
        self.assertEqual(outbox.pending(), 0)

    def test_old_results_expire_and_the_file_is_bounded(self):
        outbox.keep("old", "failed", "a", now=lambda: 0)
        self.assertEqual(outbox.flush(lambda *a: None, now=lambda: outbox.MAX_AGE_SECONDS + 10), 0)
        self.assertEqual(outbox.pending(), 0)
        for i in range(outbox.MAX_PENDING + 5):
            outbox.keep(f"t{i}", "completed", "")
        self.assertEqual(outbox.pending(), outbox.MAX_PENDING)

    def test_the_same_task_is_held_once(self):
        outbox.keep("t1", "failed", "a")
        outbox.keep("t1", "failed", "b")
        self.assertEqual(outbox.pending(), 1)


class ReportPathTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        outbox.configure(self.dir.name)

    def tearDown(self):
        outbox._dir = None

    def test_a_report_the_relay_refused_with_503_is_held(self):
        from vigil_agent import __main__ as main

        with mock.patch.object(main.client, "report_result", side_effect=http_error(503)):
            main._report_failed(object(), {"id": "t9"}, "503 Server Error on download")
        self.assertEqual(outbox.pending(), 1)
        held = (Path(self.dir.name) / outbox.STATE_FILE).read_text()
        self.assertIn("t9", held)

    def test_a_report_the_server_refused_outright_is_not_held(self):
        from vigil_agent import __main__ as main

        with mock.patch.object(main.client, "report_result", side_effect=http_error(404)):
            main._report_completed(object(), {"id": "t9"}, "ok")
        self.assertEqual(outbox.pending(), 0)


if __name__ == "__main__":
    unittest.main()
