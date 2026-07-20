"""Baseline-as-a-function: expanding ``type: baseline`` actions server-side.

A task definition may contain::

    actions:
      - type: baseline
        params:
          name: Linux bootstrap

At deploy/dispatch time the reference is replaced inline with the baseline's
own steps — the agent only ever receives concrete, allowlistable actions and
never sees the ``baseline`` type. Expansion is recursive (baselines may call
baselines) with a cycle guard and a depth cap, and the effective risk is the
max across everything that got pulled in.
"""

from __future__ import annotations

MAX_DEPTH = 5

_RISK_ORDER = {"low": 0, "standard": 1, "high": 2}


class BaselineExpandError(ValueError):
    """A baseline reference that cannot be satisfied (unknown name, cycle,
    too deep). Deploy paths surface this as a 400; auto-enroll dispatch logs
    and skips."""


def expand_actions(actions: list[dict], *, _seen: frozenset = frozenset(),
                   _depth: int = 0) -> tuple[list[dict], str]:
    """Return ``(concrete_actions, max_risk)`` with baseline refs inlined."""
    from .models import Baseline

    if _depth > MAX_DEPTH:
        raise BaselineExpandError(f"baseline nesting deeper than {MAX_DEPTH}")

    out: list[dict] = []
    max_risk = "low"

    for action in actions or []:
        if action.get("type") != "baseline":
            out.append(action)
            continue

        name = str((action.get("params") or {}).get("name", "")).strip()
        key = name.lower()
        if not name:
            raise BaselineExpandError("baseline action missing params.name")
        if key in _seen:
            raise BaselineExpandError(f"baseline cycle via {name!r}")
        baseline = Baseline.objects.filter(name__iexact=name).first()
        if baseline is None:
            raise BaselineExpandError(f"no baseline named {name!r}")

        for step in baseline.steps.select_related("definition").order_by("order"):
            spec = step.definition.parsed_spec or {}
            inner, inner_risk = expand_actions(
                spec.get("actions") or [],
                _seen=_seen | {key}, _depth=_depth + 1,
            )
            out.extend(inner)
            max_risk = _max_risk(max_risk, spec.get("risk", "standard"))
            max_risk = _max_risk(max_risk, inner_risk)

    return out, max_risk


def _max_risk(a: str, b: str) -> str:
    return a if _RISK_ORDER.get(a, 1) >= _RISK_ORDER.get(b, 1) else b
