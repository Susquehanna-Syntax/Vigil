"""The condition language an event automation filters on.

One condition is ``{"field": ..., "op": ..., "value": ...}``. A list of them
is combined with AND or OR (``Automation.condition_logic``).

Three rules run through the whole module:

* **A condition that cannot be evaluated does not match.** A bad regex, a
  number compared against text, a field the event does not carry — all read as
  "no", never as "yes". A filter someone wrote to narrow an automation must
  never widen it by failing.
* **Severity compares by rank, not alphabetically.** ``severity >= warning``
  has to mean warning-or-critical; string comparison would make "critical" the
  *smallest* severity and quietly invert the filter.
* **Text is case-insensitive.** Nobody filtering on "backup" means to be
  tripped up by "Backup".
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: The most conditions one automation may carry. Generous for real use, low
#: enough that a pasted payload cannot make evaluation expensive.
MAX_CONDITIONS = 20
MAX_VALUE_LEN = 500

TEXT = "text"
NUMBER = "number"
SEVERITY = "severity"
LIST = "list"

#: field -> (label, kind, help). ``kind`` decides which operators apply and how
#: the value is read.
FIELDS: dict[str, tuple[str, str, str]] = {
    "severity": ("Alert severity", SEVERITY, "info, warning or critical"),
    "rule": ("Alert rule name", TEXT, ""),
    "message": ("Alert text", TEXT, ""),
    "metric_value": ("Measured value", NUMBER, "the number that breached"),
    "flap_count": ("Flap count", NUMBER, "how many times it has re-fired"),
    "host": ("Hostname", TEXT, ""),
    "host_tags": ("Host tags", LIST, ""),
    "event": ("Event name", TEXT, ""),
}

#: op -> (label, kinds it applies to)
OPERATORS: dict[str, tuple[str, tuple[str, ...]]] = {
    "equals": ("is exactly", (TEXT, SEVERITY, NUMBER)),
    "not_equals": ("is not", (TEXT, SEVERITY, NUMBER)),
    "contains": ("contains", (TEXT, LIST)),
    "not_contains": ("does not contain", (TEXT, LIST)),
    "starts_with": ("starts with", (TEXT,)),
    "ends_with": ("ends with", (TEXT,)),
    "regex": ("matches regex", (TEXT,)),
    "not_regex": ("does not match regex", (TEXT,)),
    "gt": ("is greater than", (NUMBER, SEVERITY)),
    "gte": ("is at least", (NUMBER, SEVERITY)),
    "lt": ("is less than", (NUMBER, SEVERITY)),
    "lte": ("is at most", (NUMBER, SEVERITY)),
    "in": ("is one of", (TEXT, SEVERITY, NUMBER, LIST)),
    "not_in": ("is none of", (TEXT, SEVERITY, NUMBER, LIST)),
    "is_set": ("has any value", (TEXT, SEVERITY, NUMBER, LIST)),
    "is_not_set": ("is empty", (TEXT, SEVERITY, NUMBER, LIST)),
}

#: Operators that need no value at all.
VALUELESS = ("is_set", "is_not_set")

_SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}


def operators_for(field: str) -> list[str]:
    kind = FIELDS.get(field, ("", TEXT, ""))[1]
    return [op for op, (_label, kinds) in OPERATORS.items() if kind in kinds]


def meta() -> dict:
    """The field and operator surface, for the editor to build selects from."""
    return {
        "fields": [{"name": name, "label": label, "kind": kind, "help": help_,
                    "operators": operators_for(name)}
                   for name, (label, kind, help_) in FIELDS.items()],
        "operators": [{"name": op, "label": label}
                      for op, (label, _kinds) in OPERATORS.items()],
        "valueless": list(VALUELESS),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(raw) -> tuple[list[dict], str]:
    """Check a submitted condition list. Returns ``(clean, error)``.

    Invalid input is refused rather than dropped: silently discarding a
    condition would leave an automation running wider than the person who
    saved it believes.
    """
    if raw in (None, ""):
        return [], ""
    if not isinstance(raw, list):
        return [], "conditions must be a list"
    if len(raw) > MAX_CONDITIONS:
        return [], f"at most {MAX_CONDITIONS} conditions"

    clean = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return [], f"condition {index} must be an object"
        field = str(item.get("field", "")).strip()
        op = str(item.get("op", "")).strip()
        if field not in FIELDS:
            return [], f"condition {index}: unknown field {field!r}"
        if op not in OPERATORS:
            return [], f"condition {index}: unknown operator {op!r}"
        if op not in operators_for(field):
            return [], (f"condition {index}: {OPERATORS[op][0]!r} does not "
                        f"apply to {FIELDS[field][0]!r}")

        value = item.get("value", "")
        if op in VALUELESS:
            value = ""
        else:
            if isinstance(value, bool) or value is None:
                value = ""
            value = str(value).strip()
            if not value:
                return [], f"condition {index}: a value is required"
            if len(value) > MAX_VALUE_LEN:
                return [], f"condition {index}: value is too long"
            problem = _check_value(field, op, value)
            if problem:
                return [], f"condition {index}: {problem}"
        clean.append({"field": field, "op": op, "value": value})
    return clean, ""


def _check_value(field: str, op: str, value: str) -> str:
    kind = FIELDS[field][1]
    if op in ("regex", "not_regex"):
        try:
            re.compile(value)
        except re.error as exc:
            return f"invalid regex ({exc})"
        return ""
    parts = _split_list(value) if op in ("in", "not_in") else [value]
    if kind == SEVERITY and op not in ("contains", "not_contains"):
        bad = [p for p in parts if p.lower() not in _SEVERITY_RANK]
        if bad:
            return (f"severity must be one of "
                    f"{', '.join(_SEVERITY_RANK)} — got {bad[0]!r}")
    if kind == NUMBER:
        for part in parts:
            try:
                float(part)
            except ValueError:
                return f"{part!r} is not a number"
    return ""


def _split_list(value: str) -> list[str]:
    return [p.strip() for p in str(value).split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _field_value(field: str, payload: dict):
    """Pull *field* out of an event payload, or None when it is not there."""
    alert = payload.get("alert")
    host = payload.get("host") or getattr(alert, "host", None)

    if field == "event":
        return payload.get("__event__")
    if field == "host":
        return getattr(host, "hostname", None)
    if field == "host_tags":
        return list(getattr(host, "tags", None) or [])
    if alert is None:
        return None
    if field == "severity":
        return getattr(alert, "severity", None)
    if field == "rule":
        return getattr(getattr(alert, "rule", None), "name", None)
    if field == "message":
        return getattr(alert, "message", None)
    if field == "metric_value":
        return getattr(alert, "metric_value", None)
    if field == "flap_count":
        return getattr(alert, "flap_count", None)
    return None


def _as_number(field: str, value) -> float | None:
    """A comparable number for *value*, severity included."""
    if value is None:
        return None
    if FIELDS[field][1] == SEVERITY:
        return _SEVERITY_RANK.get(str(value).strip().lower())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_one(condition: dict, payload: dict) -> bool:
    field = condition.get("field", "")
    op = condition.get("op", "")
    wanted = str(condition.get("value", ""))
    if field not in FIELDS or op not in OPERATORS:
        return False

    actual = _field_value(field, payload)

    if op == "is_set":
        return actual not in (None, "", [], {})
    if op == "is_not_set":
        return actual in (None, "", [], {})
    if actual is None:
        # The event does not carry this field. "Not present" is not a match,
        # including for negative operators: a condition about an alert's
        # severity must not pass on a host-approval event.
        return False

    kind = FIELDS[field][1]

    if kind == LIST:
        items = [str(i).strip().lower() for i in (actual or [])]
        if op in ("in", "not_in"):
            wants = {w.lower() for w in _split_list(wanted)}
            hit = bool(wants & set(items))
        else:
            hit = wanted.lower() in items
        return not hit if op in ("not_contains", "not_in") else hit

    if op in ("gt", "gte", "lt", "lte"):
        left, right = _as_number(field, actual), _as_number(field, wanted)
        if left is None or right is None:
            return False
        return {"gt": left > right, "gte": left >= right,
                "lt": left < right, "lte": left <= right}[op]

    if op in ("in", "not_in"):
        wants = {w.lower() for w in _split_list(wanted)}
        hit = str(actual).strip().lower() in wants
        return not hit if op == "not_in" else hit

    text = str(actual).lower()
    needle = wanted.lower()

    if op in ("regex", "not_regex"):
        try:
            hit = bool(re.search(wanted, str(actual), re.IGNORECASE))
        except re.error:
            logger.warning("automation condition regex %r is invalid; "
                           "treating as no match", wanted)
            return False
        return not hit if op == "not_regex" else hit

    hit = {
        "equals": text.strip() == needle,
        "not_equals": text.strip() == needle,
        "contains": needle in text,
        "not_contains": needle in text,
        "starts_with": text.lstrip().startswith(needle),
        "ends_with": text.rstrip().endswith(needle),
    }.get(op, False)
    return not hit if op in ("not_equals", "not_contains") else hit


def evaluate(automation, payload: dict) -> bool:
    """True when *automation*'s conditions pass for this event payload."""
    items = automation.conditions or []
    if not items:
        return True
    results = (evaluate_one(c, payload) for c in items)
    if automation.condition_logic == automation.ConditionLogic.ANY:
        return any(results)
    return all(results)


def describe(condition: dict) -> str:
    """One condition in words, for a list row or an audit line."""
    field = FIELDS.get(condition.get("field", ""), (condition.get("field", ""),))[0]
    op = OPERATORS.get(condition.get("op", ""), (condition.get("op", ""),))[0]
    value = condition.get("value", "")
    return f"{field} {op} {value}".strip()
