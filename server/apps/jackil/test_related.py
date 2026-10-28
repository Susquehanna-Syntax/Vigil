"""Tests for the drawer's "Related in Jackil" endpoint.

Nothing here talks to a real Jackil — the HTTP layer is patched, exactly as
in ``tests.py``. What is worth proving is everything around the call: that a
host the user cannot see is refused before the far end is ever asked, and
that a helpdesk which is down, unconfigured or too old answers 200 with a
reason in it rather than breaking a drawer.
"""

from __future__ import annotations

import json
from unittest import mock

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.instance import config

RELATED = "/api/v1/jackil/related/"


def _turn_on():
    config.write("JACKIL_ENABLED", True)
    config.write("JACKIL_URL", "https://help.example.com")
    config.write("JACKIL_API_KEY", "key-123")


def _response(payload, status_code=200):
    body = json.dumps(payload).encode()
    return mock.Mock(status_code=status_code, content=body, text="",
                     json=lambda: payload)


class RelatedTests(TestCase):
    def setUp(self):
        config.invalidate()
        self.host = Host.objects.create(
            hostname="web1", agent_token="t" * 32,
            status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
        )
        self.site_scoped = get_user_model().objects.create_user("site-dana", password="pw")
        UserProfile.objects.create(user=self.site_scoped, role=Role.VIEWER)
        # Sites is a Business feature and a source checkout runs without it,
        # so the "another site" setup is guarded the way the leak tests guard
        # theirs; off that path the same denial comes from patching the helper
        # that narrows a queryset by site. Either way the point is that Jackil
        # is never called for a host the caller cannot see.
        self.sites = None
        if settings.BUSINESS_INSTALLED:
            import apps_business.sites.models

            self.sites = apps_business.sites.models
            self.sites.UserSiteRole.objects.create(
                user=self.site_scoped,
                site=self.sites.Site.objects.create(name="Lab", slug="lab"),
                role=Role.VIEWER)
        else:
            self.addCleanup(mock.patch("vigil.scoping.filter_by_site",
                                       side_effect=lambda qs, *a, **kw: qs.none()
                                       ).start())

        self.user = get_user_model().objects.create_user("dana", password="pw")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def tearDown(self):
        config.invalidate()

    def _get(self, obj_type="host", obj_id=None):
        params = {"type": obj_type}
        if obj_id is not None:
            params["id"] = obj_id
        return self.api.get(RELATED, params)

    def test_related_lists_links_and_items_for_a_visible_host(self):
        _turn_on()
        payload = {
            "type": "host", "id": str(self.host.pk),
            "links": [{"relation": "fixes", "type": "ticket", "id": "7",
                       "label": "#7 Printer on fire",
                       "url": "https://jackil.example/tickets/7/",
                       "status": "Open", "tone": "info"}],
            "items": [{"id": "3", "name": "Edge switch",
                       "url": "https://jackil.example/items/3/"}],
        }
        with mock.patch("apps.jackil.client.requests.request",
                        return_value=_response(payload)) as request:
            resp = self._get("host", str(self.host.pk))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["configured"])
        self.assertTrue(body["available"])
        self.assertEqual(
            body["links"],
            [{"relation": "fixes", "label": "#7 Printer on fire",
              "url": "https://jackil.example/tickets/7/",
              "status": "Open", "tone": "info"}])
        self.assertEqual(
            body["items"],
            [{"name": "Edge switch", "url": "https://jackil.example/items/3/"}])
        self.assertIn("/api/v1/vigil/links/?type=host&id=", request.call_args.args[1])

    def test_related_is_404_for_a_host_the_user_cannot_see(self):
        """Jackil must never be told about a host this user cannot see — the
        visibility check runs first, and a missing row answers the same way."""
        _turn_on()
        self.api.force_authenticate(user=self.site_scoped)
        with mock.patch("apps.jackil.client.requests.request") as request:
            resp_hidden = self._get("host", str(self.host.pk))
            resp_missing = self._get("host", "00000000-0000-0000-0000-000000000000")
        for resp in (resp_hidden, resp_missing):
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.json(), {"error": "Not found"})
        request.assert_not_called()

    def test_related_unknown_type_and_missing_id_are_400(self):
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request") as request:
            unknown = self._get("widget", str(self.host.pk))
            no_id = self._get("host")
            empty_id = self._get("host", "")
        for resp in (unknown, no_id, empty_id):
            self.assertEqual(resp.status_code, 400)
        self.assertEqual(unknown.json(), {"error": "Unknown type"})
        request.assert_not_called()

    def test_related_when_jackil_is_not_configured(self):
        with mock.patch("apps.jackil.client.requests.request") as request:
            resp = self._get("host", str(self.host.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"configured": False, "links": [], "items": []})
        request.assert_not_called()

    def test_related_when_jackil_is_down_says_so(self):
        """A dead helpdesk is a drawer that says why, not one that breaks."""
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request",
                        side_effect=requests.ConnectTimeout("timed out")):
            resp = self._get("host", str(self.host.pk))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["configured"])
        self.assertFalse(body["available"])
        self.assertIn("help.example.com", body["message"])
        self.assertEqual(body["links"], [])
        self.assertEqual(body["items"], [])

    def test_related_uses_the_short_timeout(self):
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request",
                        return_value=_response({"links": [], "items": []})) as request:
            resp = self._get("host", str(self.host.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(request.call_args.kwargs["timeout"], 4)

    def test_related_drops_unsafe_urls_and_unknown_tones(self):
        _turn_on()
        payload = {
            "links": [{"relation": "blocks", "label": "click me",
                       "url": "javascript:alert(1)", "status": "Hmm",
                       "tone": "hot-pink"}],
            "items": [{"name": "Cabinet", "url": "data:text/html,<script>"}],
        }
        with mock.patch("apps.jackil.client.requests.request",
                        return_value=_response(payload)):
            body = self._get("host", str(self.host.pk)).json()
        self.assertEqual(body["links"], [{"relation": "blocks", "label": "click me",
                                          "url": "", "status": "Hmm", "tone": ""}])
        self.assertEqual(body["items"], [{"name": "Cabinet", "url": ""}])

    def test_old_jackil_404_says_update_jackil(self):
        """A 404 from this call is a version problem, not a missing row."""
        _turn_on()
        with mock.patch("apps.jackil.client.requests.request",
                        return_value=_response({"detail": "Not found."}, 404)):
            body = self._get("host", str(self.host.pk)).json()
        self.assertFalse(body["available"])
        self.assertIn("Update Jackil to 2026.3 or later.", body["message"])
