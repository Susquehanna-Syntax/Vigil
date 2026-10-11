"""The host drawer adds tags through a combobox (vigil-tags.js attachTagCombobox).

It used to be a text field plus a Pick button that only filled the field: a
pick saved nothing until Add was pressed, and two picks became one tag
"a, b". The combobox lists existing tags as you type, offers to create a new
one, refuses Vigil's own inventory tags, and saves each pick at once."""
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class TagComboboxTests(SimpleTestCase):
    def setUp(self):
        self.tags = (ROOT / "static/js/vigil-tags.js").read_text(encoding="utf-8")
        self.cards = (ROOT / "static/js/vigil-host-cards.js").read_text(encoding="utf-8")
        self.base = (ROOT / "templates/base.html").read_text(encoding="utf-8")

    def test_the_drawer_field_is_a_combobox_that_saves_each_pick(self):
        self.assertIn("attachTagCombobox('detail-tag-input'", self.tags)
        self.assertNotIn("attachTagPicker('detail-tag-input'", self.tags)
        self.assertIn("async function addDetailTag(name)", self.cards)
        self.assertNotIn('id="detail-tag-add"', self.base)

    def test_inventory_tags_cannot_be_added_by_hand(self):
        self.assertIn("const _RESERVED_TAG = /^(os|os_family|pkg|arch|agent):/i;", self.tags)
        body = self.tags[self.tags.index("function attachTagCombobox("):self.tags.index("/* ── Wiring")]
        self.assertIn("disabled: true", body)
        self.assertIn("escHtml(o.name)", body)

    def test_every_tag_field_has_a_picker(self):
        self.assertIn("attachTagPicker('pol-tags'", self.tags)
