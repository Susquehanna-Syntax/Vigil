"""CVSS v3.x base score from a vector string — enough to rank an OSV record
that carries a vector but no score. Formula per FIRST's CVSS v3.1
specification, section 7."""

from __future__ import annotations

import math

_W = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_PR = {"U": {"N": 0.85, "L": 0.62, "H": 0.27}, "C": {"N": 0.85, "L": 0.68, "H": 0.5}}


def _roundup(value: float) -> float:
    whole = round(value * 100000)
    if whole % 10000 == 0:
        return whole / 100000.0
    return (math.floor(whole / 10000) + 1) / 10.0


def cvss3_base_score(vector: str) -> float | None:
    """The base score, or None when the vector is not a complete v3 one."""
    if not vector.startswith("CVSS:3"):
        return None
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        scope = m["S"]
        iss = 1 - (1 - _W["C"][m["C"]]) * (1 - _W["I"][m["I"]]) * (1 - _W["A"][m["A"]])
        if scope == "U":
            impact = 6.42 * iss
        else:
            impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
        exploit = 8.22 * _W["AV"][m["AV"]] * _W["AC"][m["AC"]] * _PR[scope][m["PR"]] * _W["UI"][m["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    if scope == "U":
        return _roundup(min(impact + exploit, 10))
    return _roundup(min(1.08 * (impact + exploit), 10))


def severity_for_score(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return "info"
