"""Line coverage per app, as a Markdown table, from ``coverage json`` output.

    python scripts/coverage-by-app.py server/coverage.json >> "$GITHUB_STEP_SUMMARY"

Visible on every run, not a gate (sprint M2 phase 6): a number to watch, not
one to game. Tests and migrations are left out of the counts.
"""
import json
import sys
from collections import defaultdict
from pathlib import PurePosixPath


def app_of(path: str) -> str | None:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if "migrations" in parts or any(p.startswith("test") for p in parts):
        return None
    for root in ("apps", "apps_business"):
        if root in parts:
            i = parts.index(root)
            if len(parts) > i + 2:          # apps/<app>/<file>
                return f"{root}/{parts[i + 1]}"
    if "vigil" in parts:
        return "vigil"
    return None


def main(path: str) -> int:
    data = json.load(open(path))
    totals = defaultdict(lambda: [0, 0])     # app -> [covered, statements]
    for file, info in data["files"].items():
        app = app_of(file)
        if app is None:
            continue
        summary = info["summary"]
        totals[app][0] += summary["covered_lines"]
        totals[app][1] += summary["num_statements"]
    covered = sum(c for c, _ in totals.values())
    statements = sum(n for _, n in totals.values())
    print("### Test coverage by app\n")
    print("| App | Lines | Covered |")
    print("| --- | ---: | ---: |")
    for app, (c, n) in sorted(totals.items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1)):
        print(f"| {app} | {n} | {100 * c / max(n, 1):.0f}% |")
    print(f"| **all** | **{statements}** | **{100 * covered / max(statements, 1):.1f}%** |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "coverage.json"))
