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
    # Ubuntu's and Debian's archives serve plain HTTP as their primary
    # mirrors; apt itself checks signatures, Vigil only reads names and
    # versions, and installing still goes through the host's own apt.
    if parts.scheme not in ("https", "http") or parts.hostname not in ALLOWED_HOSTS:
        raise FetchError(f"not an allowed catalog source: {url}")
    if parts.scheme == "http" and parts.hostname not in ("archive.ubuntu.com", "ports.ubuntu.com",
                                                           "deb.debian.org"):
        raise FetchError(f"{parts.hostname} must be fetched over HTTPS")
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
