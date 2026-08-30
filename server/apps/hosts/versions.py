"""Numeric agent-version comparison for dispatch-time safety gates.

Versions like ``2026.10.0`` must compare *numerically* per dotted segment:
the lexical string comparison says ``"2026.10.0" < "2026.9.0"`` and would
refuse exactly the hosts that are current.
"""


def version_at_least(version: str, minimum: str) -> bool:
    """True when ``version`` is >= ``minimum``, compared numerically.

    An empty, missing, or unparseable version returns False — unknown means
    too old, which means unsafe.
    """
    try:
        v = tuple(int(p) for p in str(version).split("."))
        m = tuple(int(p) for p in str(minimum).split("."))
    except (ValueError, TypeError):
        return False
    return bool(v) and v >= m
