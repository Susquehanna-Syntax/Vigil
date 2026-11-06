"""Request ids on every request and on every log line (phase 05a)."""

import json
import logging
import re
import sys

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from vigil import jsonlog, requestid

HEX32 = re.compile(r"^[0-9a-f]{32}$")
URL = "/api/v1/health/"


class RequestIdMiddlewareTests(SimpleTestCase):
    def test_a_response_carries_a_request_id(self):
        response = self.client.get(URL)
        self.assertRegex(response["X-Request-ID"], HEX32)

    def test_a_safe_incoming_id_is_kept(self):
        response = self.client.get(URL, HTTP_X_REQUEST_ID="abc-123_DEF.456")
        self.assertEqual(response["X-Request-ID"], "abc-123_DEF.456")

    def test_an_unsafe_incoming_id_is_replaced(self):
        for incoming in (
            "short",
            "x" * 65,
            "has space 12345",
            "evil\nINFO forged line",
            'quote"12345678',
        ):
            with self.subTest(incoming=incoming):
                response = self.client.get(URL, HTTP_X_REQUEST_ID=incoming)
                self.assertRegex(response["X-Request-ID"], HEX32)
                self.assertNotEqual(response["X-Request-ID"], incoming)

    def test_log_lines_inside_a_request_carry_its_id(self):
        seen = []

        def view(request):
            seen.append(requestid.current())
            return HttpResponse("ok")

        mw = requestid.RequestIdMiddleware(view)
        resp = mw(RequestFactory().get("/", HTTP_X_REQUEST_ID="req-abcdef12"))
        self.assertEqual(seen, ["req-abcdef12"])
        self.assertEqual(resp["X-Request-ID"], "req-abcdef12")
        self.assertEqual(requestid.current(), "-")

    def test_the_id_is_reset_when_the_view_raises(self):
        def view(request):
            raise ValueError("boom")

        mw = requestid.RequestIdMiddleware(view)
        with self.assertRaises(ValueError):
            mw(RequestFactory().get("/"))
        self.assertEqual(requestid.current(), "-")


class LogPlumbingTests(SimpleTestCase):
    def test_the_filter_stamps_records(self):
        record = logging.LogRecord(
            "vigil.x", logging.INFO, __file__, 1, "hello", (), None
        )
        filter_ = requestid.RequestIdFilter()
        token = requestid._current.set("rid-12345678")
        try:
            self.assertTrue(filter_.filter(record))
        finally:
            requestid._current.reset(token)
        self.assertEqual(record.request_id, "rid-12345678")

        kept = logging.LogRecord("vigil.x", logging.INFO, __file__, 1, "hi", (), None)
        kept.request_id = "keep"
        filter_.filter(kept)
        self.assertEqual(kept.request_id, "keep")

    def test_json_formatter_shape(self):
        record = logging.LogRecord(
            "vigil.x", logging.WARNING, __file__, 1, "two\nlines %s", ("here",), None
        )
        record.request_id = "rid-12345678"
        line = jsonlog.JsonFormatter().format(record)
        self.assertNotIn("\n", line)
        obj = json.loads(line)
        self.assertEqual(
            list(obj.keys()), ["ts", "level", "logger", "msg", "request_id"]
        )
        self.assertEqual(obj["msg"], "two\nlines here")
        self.assertEqual(obj["level"], "WARNING")
        self.assertTrue(obj["ts"].endswith("+00:00"))

    def test_json_formatter_includes_the_exception(self):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record = logging.LogRecord(
                "vigil.x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        line = jsonlog.JsonFormatter().format(record)
        self.assertNotIn("\n", line)
        obj = json.loads(line)
        self.assertIn("RuntimeError: boom", obj["exc"])
