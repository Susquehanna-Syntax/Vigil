"""Fetching an index: HTTPS, an allowlisted host, no redirects, a size cap.

The same rules as the KEV refresh (apps/vulns/kev.py): a redirect is a
failure, not something to follow, and the byte count is checked as the bytes
arrive because Content-Length can lie.
"""
from urllib.parse import urlsplit

#: The only hosts the catalog ever fetches from (indexes and icons).
ALLOWED_HOSTS = frozenset({
    "cdn.winget.microsoft.com",
    "dl.flathub.org",
    "archive.ubuntu.com",
    "ports.ubuntu.com",
    "deb.debian.org",
})

MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_ICON_BYTES = 512 * 1024


class FetchError(Exception):
    pass


def fetch(url: str, max_bytes: int = MAX_INDEX_BYTES) -> bytes:
    import requests

    parts = urlsplit(url)
    # HTTPS only, to the official hosts: the transport is the trust here.
    # (Installing goes through the host's own package manager, which checks
    # the packages' signatures itself.)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise FetchError(f"not an allowed catalog source: {url}")
    try:
        response = requests.get(url, timeout=(10, 120), allow_redirects=False, stream=True,
                                headers={"User-Agent": "Vigil package catalog"})
    except requests.RequestException as exc:
        raise FetchError(f"could not reach {parts.hostname}: {exc}") from None
    try:
        if response.status_code != 200:
            raise FetchError(f"{parts.hostname} answered HTTP {response.status_code}")
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise FetchError(f"{url} is {declared} bytes, over the {max_bytes} limit")
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                raise FetchError(f"{url} is over the {max_bytes}-byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    except requests.RequestException as exc:
        raise FetchError(f"{parts.hostname} failed mid-download: {exc}") from None
    finally:
        response.close()


#: Decompressed, an index is a few times its download (Ubuntu universe:
#: 19 MB → 69 MiB). A gzip that inflates past this is refused, not read.
MAX_UNPACKED_BYTES = 256 * 1024 * 1024


class _Capped:
    """A file-like over a gzip stream that refuses to inflate past a cap."""

    def __init__(self, stream, cap):
        self._stream, self._left = stream, cap

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._left + 1
        chunk = self._stream.read(min(n, self._left + 1))
        self._left -= len(chunk)
        if self._left < 0:
            raise ValueError(f"index inflates past {MAX_UNPACKED_BYTES} bytes")
        return chunk


def gunzip_stream(data: bytes, cap: int = MAX_UNPACKED_BYTES):
    """A readable over ``data`` decompressed, capped at ``cap`` bytes."""
    import gzip
    import io
    return _Capped(gzip.GzipFile(fileobj=io.BytesIO(data)), cap)


def gunzip(data: bytes, cap: int = MAX_UNPACKED_BYTES) -> bytes:
    stream, out = gunzip_stream(data, cap), []
    while chunk := stream.read(1 << 20):
        out.append(chunk)
    return b"".join(out)
