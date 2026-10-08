"""May the agent run this script file? (SEC-2)

``execute_script`` runs a named script from the scripts directory as root or
LocalSystem. That is only safe while nobody but an administrator can change
the script or anything above it up to the scripts directory: whoever can
write any of them chooses what runs with full privilege.

Linux checked only the file's own mode bits. Windows checked nothing, trusting
an installer ACL that was never applied, and C:\\ProgramData's default lets
any local user create the scripts folder and own it. Both now check the file
and every directory from it up to and including the scripts directory:

* POSIX: owned by root, and not writable by group or others.
* Windows: owned by SYSTEM, Administrators or TrustedInstaller, and no allow
  entry that applies to the object grants a write-type right to anyone else.
  Read through PowerShell with SIDs, so it works on any display language.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

#: SYSTEM, BUILTIN\Administrators, NT SERVICE\TrustedInstaller.
TRUSTED_SIDS = frozenset({
    "S-1-5-18",
    "S-1-5-32-544",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
})

#: WriteData, AppendData, WriteEA, WriteAttributes, Delete, WRITE_DAC,
#: WRITE_OWNER, and the generic write/all bits.
WRITE_MASK = (0x2 | 0x4 | 0x10 | 0x100 | 0x10000 | 0x40000 | 0x80000
              | 0x40000000 | 0x10000000)

_PS = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]

#: Owner SID and the allow entries that apply to the object itself (not
#: inherit-only ones), as JSON. {0} is the path, single quotes doubled.
_PS_ACL = (
    "$a = Get-Acl -LiteralPath '{0}'; "
    "$sid = [System.Security.Principal.SecurityIdentifier]; "
    "$rules = @($a.GetAccessRules($true, $true, $sid) | "
    "Where-Object {{ $_.AccessControlType -eq 'Allow' -and "
    "-not ($_.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) }} | "
    "ForEach-Object {{ @{{ sid = $_.IdentityReference.Value; rights = [int64]$_.FileSystemRights }} }}); "
    "@{{ owner = $a.GetOwner($sid).Value; rules = $rules }} | ConvertTo-Json -Compress -Depth 4"
)


def chain(script: Path, root: Path) -> list[Path]:
    """The script, then each directory above it, up to and including *root*."""
    out = [script]
    cur = script.parent
    while True:
        out.append(cur)
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent
    return out


def posix_problem(path: Path) -> str:
    st = path.stat()
    if st.st_uid != 0:
        return f"{path} is not owned by root"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path} is writable by group or others"
    return ""


def windows_acl(path: Path) -> dict:
    """{'owner': sid, 'rules': [{'sid', 'rights'}]} for *path*."""
    quoted = str(path).replace("'", "''")
    out = subprocess.run(_PS + [_PS_ACL.format(quoted)], capture_output=True,
                         text=True, timeout=30, check=True).stdout
    data = json.loads(out)
    rules = data.get("rules") or []
    if isinstance(rules, dict):      # ConvertTo-Json unwraps a one-item array
        rules = [rules]
    return {"owner": str(data.get("owner") or ""), "rules": rules}


def windows_problem(path: Path, acl: dict) -> str:
    if acl["owner"] not in TRUSTED_SIDS:
        return f"{path} is owned by {acl['owner'] or 'an unknown account'}, not an administrator"
    for rule in acl["rules"]:
        sid = str(rule.get("sid") or "")
        rights = int(rule.get("rights") or 0)
        if sid not in TRUSTED_SIDS and rights & WRITE_MASK:
            return f"{path} can be changed by {sid}"
    return ""


def untrusted(script: Path, root: Path) -> str:
    """'' when the script may run; otherwise why not. Fails closed: a check
    that cannot be completed is a reason to refuse."""
    for path in chain(script, root):
        try:
            if os.name == "nt":
                problem = windows_problem(path, windows_acl(path))
            else:
                problem = posix_problem(path)
        except Exception as exc:     # noqa: BLE001 — any failure refuses
            return f"could not check who can change {path}: {exc}"
        if problem:
            return problem
    return ""
