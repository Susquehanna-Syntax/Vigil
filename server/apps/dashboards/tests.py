"""Dashboards — starter layout, ownership, layout replacement, settings hygiene."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Dashboard, DashboardWidget, starter_dashboard
from .widgets import GRID_COLUMNS, WIDGET_REGISTRY


class DashboardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self.create_user(username="alice")
        self.other = self.create_user(username="bob")

    def create_user(self, username, password="pw"):
        return get_user_model().objects.create_user(username=username, password=password)

    def _login(self, user):
        self.client.force_authenticate(user)

    def _board(self, user, name="Overview", **kwargs):
        return Dashboard.objects.create(owner=user, name=name, **kwargs)

    def test_a_user_with_no_dashboard_gets_a_starter_one(self):
        self._login(self.user)
        resp = self.client.get("/api/v1/dashboards/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        self.assertTrue(Dashboard.objects.filter(owner=self.user).exists())

    def test_the_starter_dashboard_is_marked_default(self):
        self._login(self.user)
        self.client.get("/api/v1/dashboards/")
        board = Dashboard.objects.get(owner=self.user)
        self.assertTrue(board.is_default)
        self.assertEqual(board.name, "Overview")

    def test_the_starter_dashboard_has_widgets(self):
        self._login(self.user)
        resp = self.client.get("/api/v1/dashboards/")
        board = resp.json()[0]
        self.assertEqual(len(board["widgets"]), 7)
        self.assertEqual(
            {w["kind"] for w in board["widgets"]},
            {"stat_tile", "host_status_grid", "alert_list", "rollout_progress"})
        self.assertEqual(DashboardWidget.objects.filter(
            dashboard__owner=self.user).count(), 7)

    def test_creating_a_second_dashboard_does_not_steal_the_default(self):
        self._login(self.user)
        starter_dashboard(self.user)
        second = self._board(self.user, name="NOC wall")
        resp = self.client.patch(f"/api/v1/dashboards/{second.id}/", {"name": "NOC wall 2"})
        self.assertEqual(resp.status_code, 200)
        first = Dashboard.objects.get(name="Overview")
        self.assertTrue(Dashboard.objects.get(pk=first.pk).is_default)
        self.assertFalse(Dashboard.objects.get(pk=second.pk).is_default)

    def test_marking_one_default_clears_the_previous_default(self):
        self._login(self.user)
        first = self._board(self.user, name="One")
        second = self._board(self.user, name="Two")
        resp = self.client.patch(f"/api/v1/dashboards/{second.id}/", {"is_default": True})
        self.assertEqual(resp.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)
        defaults = Dashboard.objects.filter(owner=self.user, is_default=True)
        self.assertEqual(defaults.count(), 1)

    def test_two_dashboards_cannot_share_a_name_for_one_owner(self):
        self._board(self.user, name="Overview")
        self._login(self.user)
        resp = self.client.post("/api/v1/dashboards/", {"name": "Overview"}, format="json")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/v1/dashboards/", {"name": "Overview"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Dashboard.objects.filter(owner=self.user, name="Overview").count(), 1)

    def test_two_owners_may_each_have_one_called_overview(self):
        self._board(self.user, name="Overview")
        self._board(self.other, name="Overview")
        self.assertEqual(Dashboard.objects.filter(name="Overview").count(), 2)

    def test_a_stranger_cannot_read_someone_elses_dashboard(self):
        board = self._board(self.user, name="Secret", shared=False)
        self._login(self.other)
        resp = self.client.get(f"/api/v1/dashboards/{board.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_a_shared_dashboard_is_readable_by_anyone(self):
        board = self._board(self.user, name="Shared board", shared=True)
        self._login(self.other)
        resp = self.client.get(f"/api/v1/dashboards/{board.id}/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["name"], "Shared board")
        self.assertFalse(body["is_mine"])
        self.assertEqual(body["owner"], "alice")
        # shared boards show up in the other user's list
        listing = self.client.get("/api/v1/dashboards/").json()
        self.assertIn(str(board.id), [b["id"] for b in listing])

    def test_a_shared_dashboard_is_not_writable_by_a_non_owner(self):
        board = self._board(self.user, name="Shared board", shared=True)
        self._login(self.other)
        resp = self.client.patch(f"/api/v1/dashboards/{board.id}/", {"name": "Hijack"})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.delete(f"/api/v1/dashboards/{board.id}/")
        self.assertEqual(resp.status_code, 403)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": []}, format="json")
        self.assertEqual(resp.status_code, 403)
        board.refresh_from_db()
        self.assertEqual(board.name, "Shared board")

    def test_replacing_the_layout_swaps_every_widget(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        old_ids = {str(w.id) for w in board.widgets.all()}
        new_widgets = [
            {"kind": "gauge", "x": 0, "y": 0, "w": 3, "h": 3,
             "settings": {"host": "web-1", "metric": "cpu_percent"}},
            {"kind": "stat_tile", "x": 3, "y": 0, "w": 3, "h": 2, "settings": {}},
        ]
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": new_widgets}, format="json")
        self.assertEqual(resp.status_code, 200)
        board.refresh_from_db()
        widgets = list(board.widgets.all())
        self.assertEqual(len(widgets), 2)
        self.assertTrue(old_ids.isdisjoint({str(w.id) for w in widgets}))
        self.assertEqual({w.kind for w in widgets}, {"gauge", "stat_tile"})

    def test_an_unknown_widget_kind_is_refused_by_name(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": [{"kind": "not_a_widget", "x": 0, "y": 0, "w": 3, "h": 2}]},
            format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not_a_widget", resp.json()["error"])
        self.assertEqual(board.widgets.count(), 7)  # original layout intact

    def test_a_widget_wider_than_the_grid_is_clamped(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": [{"kind": "host_status_grid", "x": 0, "y": 0,
                          "w": 40, "h": 99}]}, format="json")
        self.assertEqual(resp.status_code, 200)
        widget = board.widgets.get(kind="host_status_grid")
        self.assertEqual(widget.w, GRID_COLUMNS)

    def test_a_widget_smaller_than_its_minimum_is_clamped(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": [{"kind": "host_status_grid", "x": 0, "y": 0, "w": 1, "h": 1}]},
            format="json")
        self.assertEqual(resp.status_code, 200)
        widget = board.widgets.get(kind="host_status_grid")
        self.assertEqual(widget.w, WIDGET_REGISTRY["host_status_grid"]["min_w"])
        self.assertEqual(widget.h, WIDGET_REGISTRY["host_status_grid"]["min_h"])

    def test_unknown_settings_keys_are_dropped(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": [{"kind": "alert_list", "x": 0, "y": 0, "w": 6, "h": 4,
                          "settings": {"severity": "critical",
                                       "injected": "payload",
                                       "limit": "7"}}]}, format="json")
        self.assertEqual(resp.status_code, 200)
        widget = board.widgets.get(kind="alert_list")
        # Asserted by intent rather than by whole-dict equality: the point is
        # that the injected key is gone, and every widget also carries the
        # universal ones.
        self.assertNotIn("injected", widget.settings)
        self.assertEqual(widget.settings["severity"], "critical")
        self.assertEqual(widget.settings["limit"], 7)

    def test_an_out_of_range_int_setting_is_clamped(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": [{"kind": "alert_list", "x": 0, "y": 0, "w": 6, "h": 4,
                          "settings": {"limit": 9999}}]}, format="json")
        self.assertEqual(resp.status_code, 200)
        widget = board.widgets.get(kind="alert_list")
        self.assertEqual(widget.settings["limit"], 50)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": [{"kind": "alert_list", "x": 0, "y": 0, "w": 6, "h": 4,
                          "settings": {"limit": 0}}]}, format="json")
        self.assertEqual(resp.status_code, 200)
        widget = board.widgets.get(kind="alert_list")
        self.assertEqual(widget.settings["limit"], 1)

    def test_a_choice_setting_outside_its_options_falls_back_to_the_default(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        resp = self.client.put(
            f"/api/v1/dashboards/{board.id}/layout/",
            {"widgets": [{"kind": "alert_list", "x": 0, "y": 0, "w": 6, "h": 4,
                          "settings": {"severity": "catastrophic"}}]}, format="json")
        self.assertEqual(resp.status_code, 200)
        widget = board.widgets.get(kind="alert_list")
        self.assertEqual(widget.settings["severity"], "warning")

    def test_the_catalog_lists_every_registered_widget(self):
        self._login(self.user)
        resp = self.client.get("/api/v1/dashboards/catalog/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(sorted(body["widgets"]), sorted(WIDGET_REGISTRY))
        # Compared against the registry, not a literal: the count is expected
        # to grow, and a test that has to be edited to add a widget is noise.
        self.assertEqual(len(body["widgets"]), len(WIDGET_REGISTRY))
        self.assertEqual(body["grid_columns"], GRID_COLUMNS)
        for kind, entry in body["widgets"].items():
            self.assertIn("label", entry)
            self.assertIn("w", entry)
            self.assertIn("h", entry)
            self.assertIn("min_w", entry)
            self.assertIn("min_h", entry)
            self.assertIn("settings", entry)

    def test_deleting_a_dashboard_takes_its_widgets_with_it(self):
        board = starter_dashboard(self.user)
        self._login(self.user)
        self.assertEqual(board.widgets.count(), 7)
        resp = self.client.delete(f"/api/v1/dashboards/{board.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Dashboard.objects.filter(pk=board.pk).exists())
        self.assertEqual(DashboardWidget.objects.filter(
            dashboard_id=board.pk).count(), 0)


class StarterRaceTests(TestCase):
    """The starter dashboard is created from a GET, and a fresh user's browser
    issues more than one of those before the first paint."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="racer", password="pw")

    def test_creating_the_starter_dashboard_twice_is_harmless(self):
        first = starter_dashboard(self.user)
        second = starter_dashboard(self.user)
        self.assertEqual(first.pk, second.pk)

    def test_the_second_attempt_does_not_duplicate_the_widgets(self):
        starter_dashboard(self.user)
        starter_dashboard(self.user)
        self.assertEqual(
            DashboardWidget.objects.filter(dashboard__owner=self.user).count(), 7)

    def test_listing_twice_leaves_exactly_one_dashboard(self):
        client = APIClient()
        client.force_authenticate(self.user)
        client.get("/api/v1/dashboards/")
        client.get("/api/v1/dashboards/")
        self.assertEqual(Dashboard.objects.filter(owner=self.user).count(), 1)

    def test_a_duplicate_name_is_refused_rather_than_erroring(self):
        client = APIClient()
        client.force_authenticate(self.user)
        client.post("/api/v1/dashboards/", {"name": "Twice"}, format="json")
        resp = client.post("/api/v1/dashboards/", {"name": "Twice"}, format="json")
        self.assertEqual(resp.status_code, 400)


class WidgetHeightTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="tall", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.board = starter_dashboard(self.user)

    def test_a_widget_may_be_taller_than_the_grid_is_wide(self):
        """Rows and columns are different axes. Clamping height to the column
        count capped every widget at 12 rows for no reason."""
        resp = self.client.put(
            f"/api/v1/dashboards/{self.board.id}/layout/",
            {"widgets": [{"kind": "metric_chart", "x": 0, "y": 0, "w": 6, "h": 16}]},
            format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.board.widgets.get(kind="metric_chart").h, 16)

    def test_an_absurdly_tall_widget_is_still_clamped(self):
        from .widgets import GRID_MAX_ROWS

        resp = self.client.put(
            f"/api/v1/dashboards/{self.board.id}/layout/",
            {"widgets": [{"kind": "metric_chart", "x": 0, "y": 0, "w": 6, "h": 999}]},
            format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.board.widgets.get(kind="metric_chart").h, GRID_MAX_ROWS)


class DashboardShareTests(TestCase):
    """POST /api/v1/dashboards/<id>/share/ — the Business gate on sharing."""

    def setUp(self):
        self.owner = get_user_model().objects.create_user("own", password="x")
        self.other = get_user_model().objects.create_user("oth", password="x")
        self.board = starter_dashboard(self.owner)
        self.url = f"/api/v1/dashboards/{self.board.id}/share/"

    def _post(self, user, shared):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(self.url, {"shared": shared}, format="json")

    def test_sharing_without_a_licence_answers_402(self):
        r = self._post(self.owner, True)
        self.assertEqual(r.status_code, 402)
        self.assertFalse(Dashboard.objects.get(pk=self.board.pk).shared)

    def test_the_402_body_names_the_feature_and_an_upgrade_url(self):
        r = self._post(self.owner, True)
        body = r.json()
        self.assertEqual(body["feature"], "dashboard_sharing")
        self.assertIn("upgrade_url", body)
        self.assertTrue(body["upgrade_url"].startswith("https://"))

    def test_sharing_with_a_licence_marks_the_dashboard_shared(self):
        with mock.patch("vigil.licensing.has_feature", return_value=True):
            r = self._post(self.owner, True)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["shared"])
        self.assertTrue(Dashboard.objects.get(pk=self.board.pk).shared)

    def test_unsharing_needs_no_licence(self):
        self.board.shared = True
        self.board.save(update_fields=["shared"])
        r = self._post(self.owner, False)  # no licence at all
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["shared"])
        self.assertFalse(Dashboard.objects.get(pk=self.board.pk).shared)

    def test_a_non_owner_cannot_share_someone_elses_dashboard(self):
        self.board.shared = True
        self.board.save(update_fields=["shared"])
        # 403 whether or not the instance is licensed…
        r = self._post(self.other, True)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "only the owner may share this dashboard")
        with mock.patch("vigil.licensing.has_feature", return_value=True):
            r = self._post(self.other, True)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Dashboard.objects.get(pk=self.board.pk).shared)

    def test_a_stranger_gets_404_for_a_private_dashboard_regardless_of_licence(self):
        r = self._post(self.other, True)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"error": "not found"})
        with mock.patch("vigil.licensing.has_feature", return_value=True):
            r = self._post(self.other, True)
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Dashboard.objects.get(pk=self.board.pk).shared)

    def test_reading_a_shared_dashboard_still_works_without_a_licence(self):
        self.board.shared = True
        self.board.save(update_fields=["shared"])
        client = APIClient()
        client.force_authenticate(self.other)
        r = client.get(f"/api/v1/dashboards/{self.board.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["shared"], True)
        self.assertEqual(r.json()["owner"], "own")

    def test_a_shared_dashboard_stays_readable_after_the_licence_lapses(self):
        # A lapse degrades to a banner, never to data an operator can no longer see.
        self.board.shared = True
        self.board.save(update_fields=["shared"])
        with mock.patch("vigil.licensing.has_feature", return_value=True):
            # Readable while the licence was active…
            client = APIClient()
            client.force_authenticate(self.other)
            self.assertEqual(
                client.get(f"/api/v1/dashboards/{self.board.id}/").status_code, 200)
        # …and still readable once the licence has lapsed (no patch — free tier).
        client = APIClient()
        client.force_authenticate(self.other)
        r = client.get(f"/api/v1/dashboards/{self.board.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["shared"], True)
        self.assertEqual(r.json()["owner"], "own")
        self.assertEqual(len(r.json()["widgets"]), 7)

    def test_sharing_does_not_make_the_dashboard_writable_by_others(self):
        self.board.shared = True
        self.board.save(update_fields=["shared"])
        client = APIClient()
        client.force_authenticate(self.other)
        r = client.patch(f"/api/v1/dashboards/{self.board.id}/", {"name": "hijack"},
                         format="json")
        self.assertEqual(r.status_code, 403)
        r = client.put(f"/api/v1/dashboards/{self.board.id}/layout/",
                       {"widgets": [{"kind": "metric_chart", "settings": {}}]},
                       format="json")
        self.assertEqual(r.status_code, 403)
        r = client.delete(f"/api/v1/dashboards/{self.board.id}/")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Dashboard.objects.filter(pk=self.board.pk).count(), 1)
        self.assertEqual(Dashboard.objects.get(pk=self.board.pk).name, "Overview")


class RegistryShapeTests(TestCase):
    """The rail and the settings form are generated from the registry, so a
    malformed entry is a broken screen rather than an exception."""

    GROUPS = {"fleet", "health", "work", "security", "utility", "business"}
    SETTING_TYPES = {"text", "longtext", "int", "bool", "choice",
                     "host", "metric", "playbook", "definition"}

    def test_every_widget_declares_the_fields_the_ui_reads(self):
        for kind, spec in WIDGET_REGISTRY.items():
            for field in ("group", "label", "description", "w", "h", "min_w", "min_h"):
                self.assertIn(field, spec, f"{kind} is missing {field!r}")

    def test_every_widget_is_in_a_known_group(self):
        for kind, spec in WIDGET_REGISTRY.items():
            self.assertIn(spec["group"], self.GROUPS, f"{kind} has group {spec['group']!r}")

    def test_every_setting_has_a_type_the_form_can_render(self):
        for kind, spec in WIDGET_REGISTRY.items():
            for name, field in (spec.get("settings") or {}).items():
                self.assertIn("type", field, f"{kind}.{name} has no type")
                self.assertIn(field["type"], self.SETTING_TYPES,
                              f"{kind}.{name} is type {field['type']!r}, which no form renders")
                self.assertIn("label", field, f"{kind}.{name} has no label")
                self.assertIn("default", field, f"{kind}.{name} has no default")

    def test_a_choice_setting_offers_options_containing_its_default(self):
        for kind, spec in WIDGET_REGISTRY.items():
            for name, field in (spec.get("settings") or {}).items():
                if field["type"] != "choice":
                    continue
                self.assertIn("options", field, f"{kind}.{name} is a choice with no options")
                self.assertIn(field["default"], field["options"],
                              f"{kind}.{name} defaults outside its own options")

    def test_default_sizes_respect_their_own_minimums(self):
        for kind, spec in WIDGET_REGISTRY.items():
            self.assertGreaterEqual(spec["w"], spec["min_w"], f"{kind} w < min_w")
            self.assertGreaterEqual(spec["h"], spec["min_h"], f"{kind} h < min_h")
            self.assertLessEqual(spec["w"], GRID_COLUMNS, f"{kind} is wider than the grid")

    def test_a_gated_widget_names_a_real_feature(self):
        from vigil.licensing import BUSINESS_FEATURES, FREE_FEATURES

        known = BUSINESS_FEATURES | FREE_FEATURES
        for kind, spec in WIDGET_REGISTRY.items():
            if "feature" in spec:
                self.assertIn(spec["feature"], known,
                              f"{kind} gates on {spec['feature']!r}, which no licence grants")

    def test_the_catalog_reports_the_group_and_gate_for_each_widget(self):
        user = get_user_model().objects.create_user("cat", password="x")
        client = APIClient()
        client.force_authenticate(user)
        body = client.get("/api/v1/dashboards/catalog/").json()
        self.assertEqual(len(body["widgets"]), len(WIDGET_REGISTRY))
        for kind, spec in body["widgets"].items():
            self.assertIn("group", spec, f"{kind} reaches the UI with no group")


class RailAccentTests(TestCase):
    """The rail colours each group's + button. A group the JS has no accent for
    renders an uncoloured button, which looks like a bug rather than a default.
    """

    def _dashboards_js(self) -> str:
        from django.conf import settings as s
        from pathlib import Path

        return (Path(s.BASE_DIR) / "static" / "js" / "vigil-dashboards.js").read_text()

    def test_every_group_in_the_registry_has_an_accent_and_a_label(self):
        import re

        js = self._dashboards_js()
        accents = set(re.findall(r"(\w+):\s*'(?:sky|mint|lav|peach|lemon|rose)'", js))
        labels = set(re.findall(r"(\w+):\s*'(?:Fleet|Health|Work in flight|Security|Utility|Business)'", js))
        for kind, spec in WIDGET_REGISTRY.items():
            group = spec["group"]
            self.assertIn(group, accents, f"{group!r} (used by {kind}) has no accent in the rail")
            self.assertIn(group, labels, f"{group!r} (used by {kind}) has no rail heading")

    def test_the_rail_orders_every_group_it_knows(self):
        import re

        js = self._dashboards_js()
        order = re.search(r"GROUP_ORDER = \[([^\]]+)\]", js).group(1)
        listed = set(re.findall(r"'(\w+)'", order))
        registry_groups = {spec["group"] for spec in WIDGET_REGISTRY.values()}
        self.assertTrue(registry_groups <= listed,
                        f"groups missing from GROUP_ORDER: {registry_groups - listed}")


class CardTitleTests(TestCase):
    """Every widget takes an operator-set card title.

    A dashboard may hold three metric charts; "Metric chart" three times names
    none of them.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("titler", password="x")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.board = starter_dashboard(self.user)

    def test_every_widget_accepts_a_title(self):
        from apps.dashboards.widgets import settings_for

        for kind in WIDGET_REGISTRY:
            self.assertIn("title", settings_for(kind), f"{kind} cannot be titled")

    def test_a_title_survives_a_layout_save(self):
        resp = self.client.put(
            f"/api/v1/dashboards/{self.board.id}/layout/",
            {"widgets": [{"kind": "gauge", "x": 0, "y": 0, "w": 3, "h": 3,
                          "settings": {"title": "Web CPU", "host": "h1"}}]},
            format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.board.widgets.get(kind="gauge").settings["title"], "Web CPU")

    def test_the_catalog_advertises_the_title_field(self):
        body = self.client.get("/api/v1/dashboards/catalog/").json()
        for kind, spec in body["widgets"].items():
            self.assertIn("title", spec["settings"], f"{kind} offers no title in the form")

    def test_notes_keeps_its_own_heading_separate_from_the_card_title(self):
        from apps.dashboards.widgets import settings_for

        names = settings_for("notes")
        self.assertIn("title", names)
        self.assertIn("heading", names)
        self.assertEqual(names["title"]["label"], "Card title")

    def test_an_untitled_widget_stores_an_empty_title_rather_than_dropping_it(self):
        from apps.dashboards.widgets import default_settings

        self.assertEqual(default_settings("alert_list")["title"], "")


class RendererCoverageTests(TestCase):
    """Every registered widget must have a renderer, and every renderer a
    widget. A kind with no renderer shows "no renderer for this widget"; a
    renderer with no kind is dead code nobody notices."""

    def _renderer_map(self) -> set[str]:
        import re
        from pathlib import Path

        from django.conf import settings as s

        js = (Path(s.BASE_DIR) / "static" / "js" / "vigil-widgets.js").read_text()
        block = re.search(r"const WIDGET_RENDERERS = \{(.*?)\n\};", js, re.S)
        self.assertIsNotNone(block, "WIDGET_RENDERERS is not where the test expects")
        return set(re.findall(r"^\s*(\w+):", block.group(1), re.M))

    def test_every_registered_widget_has_a_renderer(self):
        missing = set(WIDGET_REGISTRY) - self._renderer_map()
        self.assertEqual(missing, set(), f"no renderer for: {sorted(missing)}")

    def test_no_renderer_points_at_a_widget_that_does_not_exist(self):
        orphans = self._renderer_map() - set(WIDGET_REGISTRY)
        self.assertEqual(orphans, set(), f"renderers with no widget: {sorted(orphans)}")
