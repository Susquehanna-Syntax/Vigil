#!/usr/bin/env python3
"""Fail when an installed package is missing from a lock file.

The image installs requirements.txt with the lock as constraints, so a
package the lock does not name (a new dependency, or one of its own) is
installed unpinned: it escapes reproducible builds and the dependency audit.
Run inside a virtualenv made from exactly those two files:

    pip install -r server/requirements.txt -c server/requirements.lock
    python scripts/check-lock.py server/requirements.lock
"""
import re
import subprocess
import sys


def names(lines):
    out = set()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            out.add(re.split(r"[=<>!~\[; ]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return out


def main(lock_path):
    pinned = names(open(lock_path))
    frozen = subprocess.run([sys.executable, "-m", "pip", "freeze", "--exclude-editable"],
                            capture_output=True, text=True, check=True).stdout.splitlines()
    tooling = {"pip", "setuptools", "wheel"}
    missing = sorted(line for line in frozen if names([line]) - pinned - tooling)
    for line in missing:
        print(f"not in {lock_path}: {line}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
