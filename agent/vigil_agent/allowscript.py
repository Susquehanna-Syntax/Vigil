"""`vigil-agent allow-script` — approve, list and revoke inline-script hashes.

Local only, by design: a signed task that could add hashes would let the
server approve its own code, which is full_control again.
"""
import argparse
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .scripthash import normalise, script_hash

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY = "allowed_script_hashes"

#: Must match the systemd unit name install.sh writes.
_SYSTEMD_UNIT = "vigil-agent"


class _Noop(ValueError):
    """The requested change is already in place."""


def _validate_hash(raw: str) -> str:
    h = raw.strip().lower()
    if not _HASH_RE.match(h):
        raise ValueError(
            f"{raw!r} is not a valid hash — expected sha256:<64 hex chars>"
        )
    return h


def _resolve_config_path(cli: Path | None) -> Path:
    if cli is not None:
        if not cli.exists():
            raise FileNotFoundError(f"Config file {cli} does not exist")
        return cli
    env_path = os.environ.get("VIGIL_CONFIG_PATH", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    from .config import _default_config_paths

    for candidate in _default_config_paths():
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "No config file found. Tried: -c/--config, VIGIL_CONFIG_PATH, "
        f"{[str(p) for p in _default_config_paths()]}"
    )


def _find_block(lines: list[str]) -> tuple[int | None, list[int]]:
    """Return (key_index, item_indexes) for a block-style list at column 0,
    or (None, []) when the key is absent, and (None, ["flow"]) when the key
    uses flow style — which the caller refuses."""
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if stripped == f"{_KEY}:" or stripped.startswith(f"{_KEY}: #"):
            items: list[int] = []
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                if not s:
                    j += 1
                    continue
                if not lines[j].startswith((" ", "\t")):
                    break
                if s.startswith("- "):
                    items.append(j)
                    j += 1
                    continue
                if s == "-":
                    items.append(j)
                    j += 1
                    continue
                break
            return (i, items)
        if line.startswith(_KEY + ":"):
            # Flow style (`key: [a, b]`) or an inline scalar — never guess.
            return (None, ["flow"])
    return (None, [])


def _current_hashes(lines: list[str]) -> set[str]:
    key_index, items = _find_block(lines)
    if key_index is None or items == ["flow"]:
        return set()
    hashes = set()
    for i in items:
        stripped = lines[i].strip()
        if stripped.startswith("- "):
            hashes.add(stripped[2:].strip().strip("'\"").lower())
    return hashes


def _prepare_edit(path: Path, op: str, hash_str: str) -> tuple[str, set[str]]:
    """Edit the text; return (new content, expected hash set) without writing."""
    original = path.read_text(encoding="utf-8-sig")
    lines = original.split("\n")
    if original.endswith("\n"):
        lines = lines[:-1]

    key_index, items = _find_block(lines)
    if items == ["flow"]:
        raise ValueError(
            f"{_KEY} is in flow style ([...]) — convert it to a block list "
            f"of \"  - sha256:…\" lines first, then re-run"
        )

    if op == "add":
        if key_index is None:
            lines.append("")
            lines.append(f"{_KEY}:")
            lines.append(f"  - {hash_str}")
        else:
            for i in items:
                if lines[i].strip() == f"- {hash_str}":
                    raise _Noop(f"{hash_str} is already approved — nothing to do")
            insert_at = (items[-1] + 1) if items else (key_index + 1)
            lines.insert(insert_at, f"  - {hash_str}")
    else:
        if key_index is None or not items:
            raise ValueError("nothing to remove — no allowed_script_hashes entries")
        for i in items:
            if lines[i].strip() == f"- {hash_str}":
                del lines[i]
                break
        else:
            raise ValueError(f"{hash_str} is not in the allowlist")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content, _current_hashes(lines)


def _write_atomic(path: Path, content: str, expected: set[str]) -> None:
    data = content
    if not data.endswith("\n"):
        data += "\n"
    loaded = yaml.safe_load(data) or {}
    raw = loaded.get(_KEY) or []
    parsed = set()
    for entry in raw:
        h = str(entry).strip().lower()
        if _HASH_RE.match(h):
            parsed.add(h)
    if parsed != expected:
        raise ValueError(
            f"refusing to write: parsed {sorted(parsed)} != expected "
            f"{sorted(expected)}"
        )
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".agent.yml.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _restart_agent() -> None:
    if os.name == "nt":
        from .winservice import SERVICE_NAME

        cmd = ["sc.exe", "stop", SERVICE_NAME]
    else:
        cmd = ["systemctl", "restart", _SYSTEMD_UNIT]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
        print(f"Restarted the agent ({' '.join(cmd)}).")
    except (OSError, subprocess.SubprocessError) as e:
        print(
            "Could not restart the agent "
            f"({' '.join(cmd)} failed: {e}). Run it by hand — "
            "the config change is already saved."
        )


def main(argv: list[str], config_path: Path | None) -> int:
    parser = argparse.ArgumentParser(
        prog="vigil-agent allow-script",
        description="Approve, list or revoke inline-script hashes in agent.yml",
    )
    parser.add_argument("file", nargs="?", help="Script file to hash and approve")
    parser.add_argument("--hash", help="Hash to approve (sha256:<hex>)")
    parser.add_argument("--remove", help="Hash to revoke (sha256:<hex>)")
    parser.add_argument("--list", action="store_true", help="List approved hashes")
    parser.add_argument("--no-restart", action="store_true",
                        help="Do not restart the agent after editing")
    parser.add_argument("-c", "--config", type=Path, help="Path to agent.yml")
    args = parser.parse_args(argv)

    try:
        path = _resolve_config_path(args.config)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    modes = [args.file is not None, args.hash is not None,
             args.remove is not None, args.list]
    if sum(modes) != 1:
        print("error: exactly one of FILE, --hash, --remove or --list is required",
              file=sys.stderr)
        return 2

    if args.list:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        for entry in (raw.get(_KEY) or []):
            print(str(entry).strip().lower())
        return 0

    if args.file is not None:
        try:
            body = Path(args.file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            print(f"error: cannot read {args.file}: {e}", file=sys.stderr)
            return 2
        target = script_hash(body)
        print(target)
        print(f"normalised lines: {len(normalise(body).splitlines())}")
        op = "add"
    elif args.hash is not None:
        try:
            target = _validate_hash(args.hash)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        op = "add"
    else:
        try:
            target = _validate_hash(args.remove)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        op = "remove"

    try:
        content, expected = _prepare_edit(path, op, target)
        _write_atomic(path, content, expected)
    except _Noop as e:
        print(str(e))
        return 0
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if op == "add":
        print(f"approved {target}")
    else:
        print(f"revoked {target}")
    if not args.no_restart:
        _restart_agent()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], None))
