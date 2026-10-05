"""The one definition of an inline script's identity.

Byte-identical copy in server/apps/tasks/scripthash.py (phase 06b adds it and a sync test).
The agent, the `vigil-agent allow-script` CLI and the task editor must all compute the same
hash, or an approved script is refused.
"""
import hashlib

PREFIX = "sha256:"


def normalise(body: str) -> str:
    """CRLF → LF, then exactly one trailing newline."""
    return body.replace("\r\n", "\n").rstrip("\n") + "\n"


def script_hash(body: str) -> str:
    return PREFIX + hashlib.sha256(normalise(body).encode("utf-8")).hexdigest()
