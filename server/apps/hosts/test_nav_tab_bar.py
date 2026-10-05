"""The Add Host modal's Linux/Windows toggle is a .tab-bar outside any .page.

vigil-nav.js wires every .tab-bar to its enclosing .page, so before the fix
every click on that toggle threw

    TypeError: Cannot read properties of null (reading 'querySelectorAll')

(captured in the console at vigil-nav.js:33) and the modal never switched panes.
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class TabBarOutsidePage(SimpleTestCase):
    def test_tab_bar_outside_a_page_does_not_throw(self):
        js = (Path(settings.BASE_DIR) / "static" / "js" / "vigil-nav.js").read_text()
        const_group = "const group = bar.closest('.page');"
        guard = "if (!group) return;"
        query = "group.querySelectorAll('.tab-content')"
        self.assertIn(const_group, js)
        self.assertGreater(js.index(guard), js.index(const_group))
        self.assertLess(js.index(guard), js.index(query))
