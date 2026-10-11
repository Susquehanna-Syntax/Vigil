"""One module per package index. Each has ``SOURCE``, ``PLATFORM`` and
``parse(data: bytes, release: str) -> iterator of package dicts`` (pure, so
tests feed it a small sample), and sync.py does the fetching."""
