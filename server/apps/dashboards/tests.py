"""Dashboards — starter layout, ownership, layout replacement, settings hygiene."""

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
        self.assertEqual(widget.settings, {"severity": "critical", "limit": 7})

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
        self.assertEqual(len(body["widgets"]), 9)
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
