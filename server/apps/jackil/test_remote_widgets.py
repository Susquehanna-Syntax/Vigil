"""Tests for Jackil's widgets on Vigil's dashboards.

Nothing here talks to a real Jackil — the HTTP layer is patched, exactly as
in ``test_related.py``. What is worth proving is everything around that call:
that a remote catalog lands in its own group with Vigil-shaped settings, that
a helpdesk which is off or down leaves both the catalog and the tile standing
with a sentence in them instead of an exception, and that only the settings a
widget actually declared leave the building.
"""

from __future__ import annotations

import json
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.dashboards.models import Dashboard
from apps.instance import config

CATALOG_URL = "/api/v1/dashboards/catalog/"
LAYOUT_URL = "/api/v1/dashboards/"
WIDGET_URL = "/api/v1/jackil/widgets/jackil:stat_tile/"


def _turn_on():
    config.write("JACKIL_ENABLED", True)
    config.write("JACKIL_URL", "https://help.example.com")
    config.write("JACKIL_API_KEY", "key-123")


def _response(payload, status_code=200):
    body = json.dumps(payload).encode()
    return mock.Mock(status_code=status_code, content=body, text="",
                     json=lambda: payload)


CATALOG = {
    "version": 1,
    "widgets": [
        {"kind": "stat_tile", "title": "Stat tile",
         "description": "One number about the queue.", "shape": "stat",
         "settings": {
             "stat": {"type": "choice", "label": "Which number",
                      "default": "open",
                      "choices": [["open", "Open tickets"],
                                  ["unassigned", "Unassigned"]]},
             "period_days": {"type": "int", "label": "Over how many days",
                             "default": 30, "min": 1, "max": 365},
             "colour": {"type": "text", "label": "Colour", "default": "blue"},
         }},
        {"kind": "needs_attention", "title": "Needs attention",
         "description": "The tickets closest to breaching.", "shape": "list",
         "settings": {"limit": {"type": "int", "label": "How many",
                                "default": 8, "min": 1, "max": 50}}},
        # Neither Vigil nor the browser knows what to do with these.
        {"kind": "Bad Kind", "title": "Nope", "shape": "stat", "settings": {}},
        {"kind": "crystal_ball", "title": "Fortune", "shape": "oracle",
         "settings": {}},
    ],
}


def _jackil(widgets=None, answer=None, answer_status=200):
    """Patch the transport with a Jackil that answers the catalog and one tile.

    The catalog call is answered first and the tile call second, which is the
    order production code makes them in; ``answer_status`` of 404 is an unknown
    kind — a real answer from Jackil rather than an outage.
    """
    catalog = _response({"version": 1,
                         "widgets": CATALOG["widgets"] if widgets is None else widgets})

    def route(method, url, *args, **kwargs):
        if url.rstrip("/").endswith("/vigil/widgets"):
            return catalog
        if answer is None:
            return _response({"detail": "Not found."}, 404)
        return _response(answer, answer_status)

    return mock.patch("apps.jackil.client.requests.request", side_effect=route)


def _tile_url(request):
    """The URL of the call that fetched the tile, not the one for the catalog."""
    return request.call_args_list[-1].args[1]


class RemoteWidgetTests(TestCase):
    def setUp(self):
        config.invalidate()
        cache.clear()
        self.user = get_user_model().objects.create_user("dana", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def tearDown(self):
        config.invalidate()
        cache.clear()

    def test_catalog_lists_jackil_widgets_in_their_own_group(self):
        _turn_on()
        with _jackil(answer={"version": 1, "shape": "stat",
                             "data": {"value": 0}}) as request:
            resp = self.api.get(CATALOG_URL)
        self.assertEqual(resp.status_code, 200)
        widgets = resp.json()["widgets"]
        self.assertIn("https://help.example.com/api/v1/vigil/widgets/",
                      request.call_args_list[0].args[1])

        tile = widgets["jackil:stat_tile"]
        self.assertEqual(tile["group"], "jackil")
        self.assertTrue(tile["remote"])
        self.assertEqual(tile["shape"], "stat")
        self.assertEqual(tile["label"], "Stat tile")
        self.assertEqual(tile["description"], "One number about the queue.")
        self.assertEqual((tile["w"], tile["h"]), (3, 2))
        self.assertEqual((tile["min_w"], tile["min_h"]), (2, 2))
        # Vigil's own tiles keep their group and stay in the catalogue.
        self.assertNotEqual(widgets["host_status_grid"]["group"], "jackil")

        # Jackil's choice pairs become Vigil's options + labels; an int keeps
        # its bounds; a type Vigil cannot render is dropped rather than guessed.
        self.assertEqual(tile["settings"]["stat"], {
            "type": "choice", "label": "Which number", "default": "open",
            "options": ["open", "unassigned"],
            "labels": {"open": "Open tickets", "unassigned": "Unassigned"}})
        self.assertEqual(tile["settings"]["period_days"], {
            "type": "int", "label": "Over how many days", "default": 30,
            "min": 1, "max": 365})
        self.assertNotIn("colour", tile["settings"])
        # The universal title setting rides along, as on every Vigil widget.
        self.assertIn("title", tile["settings"])

        list_tile = widgets["jackil:needs_attention"]
        self.assertEqual(list_tile["shape"], "list")
        self.assertEqual((list_tile["w"], list_tile["h"]), (6, 4))
        self.assertEqual((list_tile["min_w"], list_tile["min_h"]), (3, 3))

        # Bad kind names and unknown shapes are not offered at all.
        self.assertNotIn("jackil:Bad Kind", widgets)
        self.assertNotIn("jackil:crystal_ball", widgets)
        self.assertEqual(len([k for k in widgets if k.startswith("jackil:")]), 2)

    def test_catalog_is_empty_when_jackil_is_off_or_down(self):
        with mock.patch("apps.jackil.client.requests.request") as request:
            off = self.api.get(CATALOG_URL).json()["widgets"]
        request.assert_not_called()
        self.assertFalse([k for k in off if k.startswith("jackil:")])

        _turn_on()
        cache.set("jackil:down", True, 30)
        with mock.patch("apps.jackil.client.requests.request") as request:
            down = self.api.get(CATALOG_URL).json()["widgets"]
        request.assert_not_called()
        self.assertFalse([k for k in down if k.startswith("jackil:")])

    def test_catalog_is_empty_when_jackil_does_not_answer(self):
        """An unreachable helpdesk marks itself down rather than raising."""
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request",
                        side_effect=requests.ConnectTimeout("timed out")):
            widgets = self.api.get(CATALOG_URL).json()["widgets"]
        self.assertFalse([k for k in widgets if k.startswith("jackil:")])
        self.assertTrue(cache.get("jackil:down"))

    def test_catalog_is_cached(self):
        _turn_on()
        with _jackil(answer={"version": 1, "shape": "stat",
                             "data": {"value": 0}}) as request:
            first = self.api.get(CATALOG_URL).json()["widgets"]
            second = self.api.get(CATALOG_URL).json()["widgets"]
        self.assertEqual(first, second)
        self.assertEqual(len(request.call_args_list), 1)

    def test_remote_widget_sends_only_declared_cleaned_settings(self):
        _turn_on()
        with _jackil(answer={"version": 1, "shape": "stat",
                             "data": {"value": 7, "label": "Open tickets"}}) as request:
            resp = self.api.get(WIDGET_URL, {
                "stat": "unassigned",       # a declared, known choice
                "period_days": "900",       # clamped to the declared max
                "title": "My queue",        # universal — not Jackil's setting
                "evil": "../../etc/passwd",  # undeclared
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "version": 1, "shape": "stat",
            "data": {"value": 7, "label": "Open tickets"}})

        self.assertEqual(len(request.call_args_list), 2)  # catalog, then the tile
        url = _tile_url(request)
        self.assertIn("/api/v1/vigil/widgets/stat_tile/", url)
        self.assertEqual(parse_qs(urlparse(url).query),
                         {"stat": ["unassigned"], "period_days": ["365"]})

    def test_remote_widget_falls_back_to_declared_defaults(self):
        """Nothing posted means the widget's own defaults, not nothing at all."""
        _turn_on()
        with _jackil(answer={"version": 1, "shape": "stat",
                             "data": {"value": 1}}) as request:
            resp = self.api.get(WIDGET_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(parse_qs(urlparse(_tile_url(request)).query),
                         {"stat": ["open"], "period_days": ["30"]})

    def test_remote_widget_sends_nothing_when_jackil_is_off(self):
        """Jackil can be off but the tile still has to answer with a sentence."""
        with mock.patch("apps.jackil.client.requests.request") as request:
            resp = self.api.get(WIDGET_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "version": 1, "shape": "stat",
            "data": {"value": 0, "label": "Jackil is not connected.",
                     "tone": "muted"}})
        request.assert_not_called()

    def test_remote_widget_down_is_a_plain_stat_and_stops_calling(self):
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request",
                        side_effect=requests.ConnectTimeout("timed out")) as request:
            first = self.api.get(WIDGET_URL).json()
            second = self.api.get(WIDGET_URL)
        self.assertEqual(first["shape"], "stat")
        self.assertEqual(first["data"]["label"], "Jackil is not answering right now.")
        self.assertEqual(second.json(), first)
        # The 30-second "stop calling" flag: this request asked once, the next
        # one asks nothing at all.
        self.assertEqual(len(request.call_args_list), 1)

    def test_unknown_remote_widget_says_it_no_longer_exists(self):
        _turn_on()
        with _jackil(answer=None):
            body = self.api.get(WIDGET_URL).json()
        self.assertEqual(body["shape"], "stat")
        self.assertEqual(body["data"]["label"],
                         "This Jackil widget no longer exists.")

    def test_bad_shape_becomes_a_message(self):
        _turn_on()
        for answer in ({"version": 1, "shape": "oracle", "data": {}},
                       {"version": 1, "shape": "stat", "data": "nope"}):
            with _jackil(answer=answer):
                body = self.api.get(WIDGET_URL).json()
            self.assertEqual(body["shape"], "stat")
            self.assertEqual(body["data"]["label"],
                             "Jackil sent a widget Vigil could not draw.")

    def test_jackil_widget_endpoint_needs_a_login(self):
        anon = APIClient()
        resp = anon.get(WIDGET_URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_non_remote_kind_is_404(self):
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request") as request:
            resp = self.api.get("/api/v1/jackil/widgets/alert_list/")
        self.assertEqual(resp.status_code, 404)
        request.assert_not_called()

    def test_layout_keeps_a_jackil_tile_while_jackil_is_down(self):
        """The tile is in no registry right now; the layout keeps it anyway."""
        board = Dashboard.objects.create(owner=self.user, name="Main")
        cache.set("jackil:down", True, 30)
        with mock.patch("apps.jackil.client.requests.request") as request:
            resp = self.api.put(f"{LAYOUT_URL}{board.id}/layout/", {
                "widgets": [{"kind": "jackil:stat_tile", "x": 0, "y": 0,
                             "w": 3, "h": 2, "settings": {"title": "Queue"}}],
            }, format="json")
        request.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        saved = resp.json()["widgets"][0]
        self.assertEqual(saved["kind"], "jackil:stat_tile")
        self.assertEqual((saved["w"], saved["h"]), (3, 2))

    def test_layout_clamps_a_jackil_tile_to_its_minimum(self):
        board = Dashboard.objects.create(owner=self.user, name="Main")
        cache.set("jackil:down", True, 30)
        with mock.patch("apps.jackil.client.requests.request"):
            resp = self.api.put(f"{LAYOUT_URL}{board.id}/layout/", {
                "widgets": [{"kind": "jackil:stat_tile", "x": 0, "y": 0,
                             "w": 1, "h": 1}],
            }, format="json")
        self.assertEqual(resp.status_code, 200)
        saved = resp.json()["widgets"][0]
        self.assertEqual((saved["w"], saved["h"]), (2, 2))

    def test_layout_still_refuses_a_kind_that_is_nothing_at_all(self):
        board = Dashboard.objects.create(owner=self.user, name="Main")
        resp = self.api.put(f"{LAYOUT_URL}{board.id}/layout/", {
            "widgets": [{"kind": "jackil:../../etc", "x": 0, "y": 0,
                         "w": 3, "h": 2}],
        }, format="json")
        self.assertEqual(resp.status_code, 400)
