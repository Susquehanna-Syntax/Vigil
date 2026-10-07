"""The Firewall page must open on a host, not on "Choose a host…".

The host control used to be a <select>, whose first <option> the browser
auto-selects. It is now a picker button plus a hidden <input>, which ignores
<option> children — so the code that still appended options left the value
"" and the page loaded no snapshot at all. These tests read the JS as text;
they run without a browser.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static" / "js" / "vigil-firewall.js").read_text(encoding="utf-8")

_FUNC_START = re.compile(r"^(?:async )?function (?P<name>\w+)\(", re.MULTILINE)


def _source_of(name: str) -> str:
    """The body of a top-level function, by name.

    Slicing from the function's declaration to the next top-level function
    declaration is all this file needs: the assertions are about which
    statements the function contains.
    """
    match = re.search(rf"^(?:async )?function {re.escape(name)}\(", JS, re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} not found in vigil-firewall.js")
    rest = JS[match.start():]
    nxt = _FUNC_START.search(rest, len(f"function {name}("))
    return rest if nxt is None else rest[:nxt.start()]


class FirewallPageHostSelectionTests(SimpleTestCase):

    def test_no_options_on_a_hidden_input(self):
        # The host control is a picker button plus a hidden <input>; options
        # and a `change` listener on it are the bug this phase fixes. The
        # rule-policy and add-rule dropdowns further down the file are real
        # <select>s that legitimately build <option>s, and `replaceChildren`
        # there is a rendering idiom used by a different element — so the
        # assertion is scoped to the function that owns the host control.
        body = _source_of("_fwPopulateHosts")
        self.assertNotIn("createElement('option')", body)
        self.assertNotIn("sel.replaceChildren()", body)
        self.assertNotIn("sel.appendChild", body)
        self.assertNotIn("sel.addEventListener('change'", body)

    def test_starts_on_a_host(self):
        body = _source_of("_fwPopulateHosts")
        self.assertIn("_fwRecall()", body)
        self.assertIn("const start = eligible.find(h => String(h.id) === wanted) || eligible[0]", body)
        self.assertIn("sel.value = start.id", body)
        self.assertIn("textContent = start.hostname", body)

    def test_every_storage_read_is_guarded(self):
        # A private window makes localStorage throw on access, not only on write.
        for name in ("_fwRemember", "_fwRecall"):
            self.assertIn("try {", _source_of(name))

    def test_remembers_the_choice(self):
        self.assertIn("_fwRemember(item.key)", _source_of("pickFirewallHost"))
        self.assertIn("try { localStorage.setItem(FW_HOST_KEY, id); }", _source_of("_fwRemember"))
        self.assertIn("try { return localStorage.getItem(FW_HOST_KEY) || ''; }", _source_of("_fwRecall"))
