"""winget: the community repository's index, a SQLite file inside source2.msix.

Tables used: ``packages(rowid, id, name, latest_version)``,
``norm_publishers2(norm_publisher, package)`` and ``tags2(rowid, tag)`` +
``tags2_map(tag, package)``. The index has no descriptions or icons.
"""
import io
import os
import re
import sqlite3
import tempfile
import zipfile

SOURCE = "winget"
PLATFORM = "windows"
URL = "https://cdn.winget.microsoft.com/cache/source2.msix"
MAX_DB_BYTES = 256 * 1024 * 1024
#: A language tag as the last part of an id ("de", "en-GB", "ca-valencia").
_LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,8})*$")
#: "Mozilla Firefox (en-US)" → "Mozilla Firefox".
_LOCALE_SUFFIX = re.compile(r"\s*\([a-z]{2,3}(-[A-Za-z]{2,8})*\)$")


def parse(data: bytes, release: str = ""):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("Public/index.db")
        if info.file_size > MAX_DB_BYTES:
            raise ValueError("winget index.db is unexpectedly large")
        db = z.read(info)
    fd, path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(db)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            publishers = dict(conn.execute(
                "SELECT package, MIN(norm_publisher) FROM norm_publishers2 GROUP BY package"))
            tags: dict = {}
            for pkg, tag in conn.execute(
                    "SELECT m.package, t.tag FROM tags2_map m JOIN tags2 t ON t.rowid = m.tag"):
                tags.setdefault(pkg, []).append(tag)
            rows = list(conn.execute("SELECT rowid, id, name, latest_version FROM packages"))
            ids = {r[1].lower() for r in rows if r[1]}
            for rowid, pid, name, version in rows:
                if not pid or not name:
                    continue
                # One package per language (Mozilla.Firefox.DeveloperEdition.de …)
                # beside the base one is the same app ninety times over.
                base, _, last = pid.rpartition(".")
                if base and _LOCALE.match(last) and base.lower() in ids:
                    continue
                yield {
                    "package_id": pid, "name": _LOCALE_SUFFIX.sub("", name) or name,
                    "version": version or "",
                    # The id's prefix keeps its capitals ("Mozilla"); the
                    # index's publisher column is normalised to lower case.
                    "publisher": pid.split(".", 1)[0] if "." in pid else publishers.get(rowid, ""),
                    "categories": sorted(set(tags.get(rowid, [])))[:8],
                }
        finally:
            conn.close()
    finally:
        os.unlink(path)
