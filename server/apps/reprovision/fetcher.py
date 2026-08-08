"""Fetch an ISO into the library, then hand it to the existing importer.

One pass: the payload is hashed as it streams to disk rather than being read
back afterwards, because these files are gigabytes and reading them twice
doubles the slowest part of the operation.

Every exit that is not success deletes the temp file. A half-downloaded image
that looks present is worse than none — the operator picks it for a rebuild
and discovers the truth at the point of no return, when the target machine's
disk is already being wiped.

Known gap (documented, not fixed here): there is a DNS-rebind TOCTOU between
`fetch_guard.check_url` resolving the hostname and `requests` resolving it
again when it actually connects. An attacker controlling DNS could answer
the guard's lookup with a safe address and the connection's lookup with a
blocked one (e.g. cloud instance metadata). Closing this needs a custom
transport adapter that pins the address resolved by the guard through the
connection itself, which is out of scope for this task.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests

from .fetch_guard import check_url
from .images import image_root, import_iso
from .models import OSImage

logger = logging.getLogger("vigil.reprovision")

_CHUNK = 1024 * 1024
_TIMEOUT = (10, 60)  # connect, read
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _RedirectRefused(Exception):
    """A hop in the redirect chain landed somewhere the guard refuses."""


def _connect(start_url: str):
    """GET *start_url*, following redirects by hand.

    `requests`' own `allow_redirects=True` connects to every hop in the
    chain before any code gets a chance to look at where it landed — the
    guard would only ever see the final URL, after Vigil already originated
    a GET against every address in between. That is a blind-SSRF primitive
    in its own right (side effects, internal probing by timing), even though
    the response bodies of the intermediate hops are never surfaced and a
    swapped final payload still fails the checksum.

    So each hop is resolved one at a time, and `check_url` runs on a hop's
    *destination* before this function ever connects to it — the same
    contract `fetch_guard.check_url`'s docstring already promises callers
    ("the caller re-checks after every redirect"). Capped at
    `_MAX_REDIRECTS` so a redirect loop cannot hang a worker.

    Returns the terminal (non-redirect) response, still open for the caller
    to stream. Raises `_RedirectRefused` if a hop is refused, or a plain
    exception for a missing `Location` header or too many hops — both land
    on the image as FAILED via the caller's exception handling.
    """
    url = start_url
    for _ in range(_MAX_REDIRECTS + 1):
        response = requests.get(url, stream=True, timeout=_TIMEOUT,
                                allow_redirects=False)
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise ValueError(f"Redirect from {url} had no Location header")
        url = urljoin(url, location)
        if refusal := check_url(url):
            raise _RedirectRefused(refusal)
    raise ValueError(f"Too many redirects (more than {_MAX_REDIRECTS})")


def _fail(image: OSImage, reason: str, temp: Path | None) -> None:
    """Land the image on FAILED and make sure nothing was left on disk."""
    if temp is not None:
        temp.unlink(missing_ok=True)
    image.status = OSImage.Status.FAILED
    image.import_error = reason
    image.save(update_fields=["status", "import_error"])
    logger.warning("Image %s failed: %s", image.id, reason)


def download_and_import(image_id) -> None:
    """Download, verify, import. Never raises — the outcome is on the row."""
    image = None
    temp: Path | None = None
    try:
        image = OSImage.objects.filter(pk=image_id).first()
        if image is None:
            return

        # Two refusals cheap enough to make before a single byte moves.
        # images.verify_checksum refuses an empty digest anyway; discovering
        # that after several gigabytes would be both wasteful and an unkind
        # error message.
        if not image.sha256:
            return _fail(
                image,
                "No SHA-256 recorded — refusing to fetch an image whose "
                "bytes cannot be verified.",
                None,
            )
        if refusal := check_url(image.source_url):
            return _fail(image, refusal, None)

        root = image_root()
        root.mkdir(parents=True, exist_ok=True)
        temp = root / f".download-{image.id}.iso"

        digest = hashlib.sha256()
        written = 0
        try:
            connection = _connect(image.source_url)
        except _RedirectRefused as exc:
            return _fail(
                image,
                f"Redirected to a refused destination: {exc}",
                temp,
            )
        with connection as response:
            response.raise_for_status()
            with temp.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=_CHUNK):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    # Cheap enough at ~1 MiB granularity, and the progress
                    # bar needs it to actually move rather than jump at
                    # the end.
                    OSImage.objects.filter(pk=image.pk).update(
                        bytes_downloaded=written)

        actual = digest.hexdigest()
        if actual.lower() != image.sha256.lower():
            return _fail(
                image,
                f"SHA-256 checksum mismatch: expected {image.sha256}, "
                f"got {actual}",
                temp,
            )

        # import_iso (apps/reprovision/images.py) verifies the checksum a
        # second time, extracts kernel/initrd/tree, and never raises on an
        # internal failure — it records FAILED on the image itself and
        # returns normally. So only an exception escaping the call means the
        # call itself blew up; anything import_iso decided is already on the
        # row once it returns, and must not be overwritten below.
        import_iso(image, temp)
        temp.unlink(missing_ok=True)

        image.refresh_from_db()
        if image.status == OSImage.Status.FAILED:
            OSImage.objects.filter(pk=image.pk).update(bytes_downloaded=written)
            return
        image.status = OSImage.Status.READY
        image.bytes_downloaded = written
        image.import_error = ""
        image.save(update_fields=["status", "bytes_downloaded", "import_error"])
    except Exception as exc:  # noqa: BLE001 — a bad fetch must not take the worker down
        logger.exception("Image fetch failed for %s", image_id)
        if image is not None:
            _fail(image, str(exc), temp)
