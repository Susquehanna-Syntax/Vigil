from .base import Renderer, enrolment_script, merged_packages, password_hash
from .debian import DebianRenderer
from .rhel import RhelRenderer
from .ubuntu import UbuntuRenderer

_REGISTRY = {r.family: r() for r in (UbuntuRenderer, DebianRenderer, RhelRenderer)}


def get_renderer(family: str) -> Renderer:
    """Look up the renderer for *family*.

    Raises KeyError if unknown: an unsupported family must fail loudly at
    ceremony time, never render a partial file that an installer would
    happily boot into and then stop at a prompt.
    """
    return _REGISTRY[family]


__all__ = ["Renderer", "get_renderer", "enrolment_script", "merged_packages",
           "password_hash"]
