"""The dashboard's stylesheets as one text, for tests that read the shipped CSS.

The design is split into numbered parts under static/css/vigil/, loaded by
templates/_stylesheets.html in that order. Joining them in the same order gives
exactly the cascade a browser sees, so a test can search one string for a rule
without knowing which part holds it.
"""

import re
from pathlib import Path

from django.conf import settings

CSS_DIR = Path(settings.BASE_DIR) / "static" / "css"
INCLUDE = Path(settings.BASE_DIR) / "templates" / "_stylesheets.html"


def stylesheet_paths() -> list[Path]:
    """The design's parts (not tokens.css), in the order _stylesheets.html links them."""
    hrefs = re.findall(r"""\{% static ['"]css/(vigil/[^'"]+\.css)['"] %\}""", INCLUDE.read_text())
    return [CSS_DIR / href for href in hrefs]


def design_css() -> str:
    """Every part of the design, joined in cascade order (tokens.css not included)."""
    return "".join(path.read_text(encoding="utf-8") for path in stylesheet_paths())
