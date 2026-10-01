"""Fix groups: findings grouped by the one action that resolves them (M9).

"App Y has vulnerability X, with CVEs 1-100" is one problem with one fix —
upgrade App Y to the fixed version — so it is one critical, not a hundred.
A finding's ``fix_key`` names that fix:

* a package with a fixed version — ``openssl@3.0.13-0ubuntu3.1``;
* a package with no fix yet — ``openssl@nofix``;
* a language package or jar — the same, plus the file it lives in, since
  two copies on disk are two things to fix;
* anything else (a network plugin with no package) — the scanner's own
  finding id: that plugin's remediation is its own fix.

The key holds no host; grouping per host is the scorer's job, grouping
across hosts the remediation view's. Pure functions only: the data
migrations call them with frozen models.
"""

from __future__ import annotations


def fix_key_for(package_name: str, fixed_version: str, scanner: str,
                plugin_id_or_oid: str, affected_path: str = "") -> str:
    if plugin_id_or_oid.startswith("det:"):
        # A detection task is its own fix: every CVE it names is cleared by
        # deploying it (M10).
        return ":".join(plugin_id_or_oid.split(":")[:2])[:300]
    pkg = (package_name or "").strip().lower()
    if pkg:
        key = f"{pkg}@{(fixed_version or '').strip() or 'nofix'}"
        if affected_path:
            key += f"|{affected_path.strip()}"
        return key[:300]
    return f"{scanner}:{plugin_id_or_oid}"[:300]


def group(findings) -> list[list]:
    """Partition findings into fix groups: same ``fix_key``, and — so one CVE
    reported by two scanners is not counted twice — any two groups that share
    a CVE are merged. Works on model instances, frozen or not."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    findings = list(findings)
    for f in findings:
        key = ("fix", f.fix_key or f"{f.scanner}:{f.plugin_id_or_oid}")
        find(key)
        cve = (f.cve_id or "").strip().upper()
        if cve:
            union(key, ("cve", cve))
    groups: dict = {}
    for f in findings:
        root = find(("fix", f.fix_key or f"{f.scanner}:{f.plugin_id_or_oid}"))
        groups.setdefault(root, []).append(f)
    return list(groups.values())
