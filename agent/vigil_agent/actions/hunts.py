"""Hunt actions: read-only discovery probes (M5).

Moved out of executor.py. The probes themselves live in ``vigil_agent.hunt``;
these handlers are one-liners that hand the probe to ``run_hunt``, which owns
the result shape, the result/timeout caps and the priority lowering.
"""

from __future__ import annotations

from .. import hunt
from ..config import AgentConfig


def _hunt_file(params: dict, _config: AgentConfig) -> str:
    """Find files by name/glob and optional sha256, size and age.

    Walks the targeted scope by default (every filesystem root with
    ``scope: full``, or the given ``paths``) and adds one ``file`` match per hit. Read-only: it never modifies,
    moves or deletes anything it finds.
    """
    return hunt.run_hunt(hunt.hunt_file, params)
