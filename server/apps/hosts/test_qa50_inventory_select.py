"""Inventory cells can be selected and copied; only the host name opens the
detail panel (QA-50, user 2026-10-16)."""

from pathlib import Path

from django.test import SimpleTestCase

from apps.reports.test_reports_ui import _code_only, _function

ROOT = Path(__file__).resolve().parents[2]
JS = _code_only((ROOT / "static/js/vigil-inventory.js").read_text())
CSS = (ROOT / "static/css/vigil.css").read_text()


class InventorySelectTests(SimpleTestCase):

    def test_a_row_click_opens_nothing(self):
        body = _function(JS, "renderInventory")
        tbody = body[body.index("const tbody"):]
        self.assertNotIn("addEventListener('click'", tbody)
        self.assertNotIn("openInventoryDetail", tbody)

    def test_the_host_name_is_a_button_that_opens_the_panel(self):
        cell = _function(JS, "_hostCell")
        self.assertIn("document.createElement('button')", cell)
        self.assertIn("name.type = 'button'", cell)
        self.assertIn("openInventoryDetail(r)", cell)

    def test_the_host_column_cannot_be_hidden(self):
        self.assertIn("ids.add('hostname')", _function(JS, "_invVisibleIds"))
        self.assertIn("cb.disabled = true", _function(JS, "toggleInvColEditor"))

    def test_rows_no_longer_look_clickable(self):
        self.assertIn(".inv-table tbody tr { transition: background 0.12s; }", CSS)
        self.assertIn(".inv-table tbody td { user-select: text; cursor: text; }", CSS)
