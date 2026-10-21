"""Per-tenant on-disk FIFO buffer for agent requests recorded while the uplink is down.

Frames are the same JSON objects the relay would have forwarded, so replay after
reconnect sends exactly what the agent asked. Nothing but the frame lives on disk;
file permissions are 0o600 inside a 0o700 directory.
"""

import hashlib
import json
import os
import time
import uuid


class DiskQueue:
    #: How many queued frames one sender may hold.
    PER_OWNER = 500

    def __init__(self, dirpath: str, max_bytes: int, max_items: int) -> None:
        self.dirpath = dirpath
        self.max_bytes = max_bytes
        self.max_items = max_items
        os.makedirs(dirpath, mode=0o700, exist_ok=True)

    def put(self, frame: dict, owner: str = "") -> bool:
        """Persist a frame, then trim the oldest entries until both limits hold.
        Returns False (writing nothing) when the frame alone exceeds ``max_bytes``."""
        data = json.dumps(frame).encode()
        if len(data) > self.max_bytes:
            return False
        tag = hashlib.sha256(owner.encode()).hexdigest()[:12]
        if sum(1 for p in self.items() if os.path.basename(p).split("-")[1:2] == [tag]) >= self.PER_OWNER:
            return False
        # Wall-clock, not monotonic: the monotonic clock restarts at boot, and
        # the queue has to keep its order across a relay restart.
        path = os.path.join(self.dirpath, f"{time.time_ns():020d}-{tag}-{uuid.uuid4().hex}.json")
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        self._trim()
        return True

    def items(self) -> list[str]:
        """Queued frame paths, oldest first."""
        try:
            names = [n for n in os.listdir(self.dirpath) if n.endswith(".json")]
        except FileNotFoundError:
            return []
        return [os.path.join(self.dirpath, n) for n in sorted(names)]

    def load(self, path: str) -> dict:
        with open(path) as fh:
            return json.load(fh)

    def drop(self, path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _trim(self) -> None:
        paths = self.items()
        total = sum(_size(p) for p in paths)
        while paths and (len(paths) > self.max_items or total > self.max_bytes):
            total -= _size(paths[0])
            self.drop(paths.pop(0))


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
