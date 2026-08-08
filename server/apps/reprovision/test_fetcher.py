"""Downloading an image must never leave the library dirty.

Disk is the scarce resource in this feature and a half-imported image that
looks present is worse than none — the operator picks it for a rebuild and
finds out at the point of no return.
"""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import OSImage

PAYLOAD = b"not really an iso, but it hashes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class _FakeResponse:
    """Minimal stand-in for requests' streaming response."""

    def __init__(self, chunks, status=200, url="https://mirror.example.com/x.iso"):
        self._chunks = chunks
        self.status_code = status
        self.headers = {"content-length": str(sum(len(c) for c in chunks))}
        self.url = url

    def iter_content(self, chunk_size):
        yield from self._chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RedirectResponse:
    """A real 302 (or similar), Location header and all — not just a
    response whose `.url` has already been rewritten. Exercises the
    fetcher's own redirect-following, the only way to prove it re-checks
    the guard before connecting to each hop rather than after the fact."""

    def __init__(self, status, location=None, chunks=(PAYLOAD,)):
        self.status_code = status
        self.headers = {}
        if location is not None:
            self.headers["Location"] = location
        else:
            self.headers["content-length"] = str(sum(len(c) for c in chunks))
        self._chunks = chunks
        self.url = location or "https://mirror.example.com/x.iso"
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self._chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _BoomMidStream:
    """Simulates a network error partway through the response body."""

    status_code = 200
    headers = {"content-length": "999"}
    url = "https://mirror.example.com/x.iso"

    def iter_content(self, chunk_size):
        yield PAYLOAD[:4]
        raise ConnectionError("connection reset by peer")

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetcherTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _image(self, sha=DIGEST):
        return OSImage.objects.create(
            name="Test", os_family="ubuntu", version="1", sha256=sha,
            source_url="https://mirror.example.com/x.iso",
            status=OSImage.Status.DOWNLOADING)

    def _run(self, image, chunks=(PAYLOAD,), importer=None):
        from . import fetcher

        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", lambda *a, **k: ""), \
             patch.object(fetcher.requests, "get",
                          lambda *a, **k: _FakeResponse(list(chunks))), \
             patch.object(fetcher, "import_iso", importer or (lambda *a, **k: None)):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()
        return image

    def _leftovers(self):
        return [p.name for p in Path(self.tmp.name).iterdir()]

    def test_a_good_download_reaches_ready(self):
        image = self._run(self._image())
        self.assertEqual(image.status, OSImage.Status.READY)

    def test_progress_is_recorded(self):
        image = self._run(self._image())
        self.assertEqual(image.bytes_downloaded, len(PAYLOAD))

    def test_a_checksum_mismatch_fails_the_image(self):
        image = self._run(self._image(sha="0" * 64))
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("checksum", image.import_error.lower())

    def test_a_checksum_mismatch_leaves_no_temp_file(self):
        """The download is gigabytes. Leaving it behind fills the volume."""
        self._run(self._image(sha="0" * 64))
        self.assertEqual(self._leftovers(), [])

    def test_a_failed_import_fails_the_image_and_cleans_up(self):
        def boom(*a, **k):
            raise RuntimeError("bad iso structure")

        image = self._run(self._image(), importer=boom)
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("bad iso structure", image.import_error)
        self.assertEqual(self._leftovers(), [])

    def test_an_importer_that_records_its_own_failure_is_not_overwritten_to_ready(self):
        """import_iso (the real one) never raises on an internal failure — it
        records FAILED and import_error on the image itself and returns
        normally. A no-op mock is indistinguishable from "hasn't happened
        yet", so this drives the importer stand-in to actually behave like
        the real thing: fail the image itself, without raising."""

        def quietly_fails(image, iso_path):
            image.status = OSImage.Status.FAILED
            image.import_error = "corrupt ISO9660 tree"
            image.save(update_fields=["status", "import_error"])

        image = self._run(self._image(), importer=quietly_fails)
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertEqual(image.import_error, "corrupt ISO9660 tree")
        self.assertEqual(self._leftovers(), [])

    def test_a_refused_url_never_downloads(self):
        from . import fetcher

        called = []
        image = self._image()
        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", lambda *a, **k: "nope"), \
             patch.object(fetcher.requests, "get",
                          lambda *a, **k: called.append(1)):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()
        self.assertEqual(called, [])
        self.assertEqual(image.status, OSImage.Status.FAILED)

    def test_an_empty_digest_is_refused_before_downloading(self):
        """images.verify_checksum already refuses these; the fetcher must not
        spend gigabytes discovering it."""
        from . import fetcher

        called = []
        image = self._image(sha="")
        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", lambda *a, **k: ""), \
             patch.object(fetcher.requests, "get",
                          lambda *a, **k: called.append(1)):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()
        self.assertEqual(called, [])
        self.assertEqual(image.status, OSImage.Status.FAILED)

    def test_the_iso_is_not_kept_after_a_successful_import(self):
        self._run(self._image())
        self.assertEqual(self._leftovers(), [])

    def test_a_network_error_mid_stream_leaves_no_temp_file(self):
        from . import fetcher

        image = self._image()
        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", lambda *a, **k: ""), \
             patch.object(fetcher.requests, "get",
                          lambda *a, **k: _BoomMidStream()):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertTrue(image.import_error)
        self.assertEqual(self._leftovers(), [])

    def test_a_redirect_to_a_refused_destination_is_never_connected_to(self):
        """The guard must run on a hop's destination BEFORE the fetcher
        connects to it -- not on `response.url` after the fact, by which
        point the GET against the refused address would already have gone
        out. This is what distinguishes a real guard from a decoration."""
        from . import fetcher

        refused = "http://169.254.169.254/x.iso"
        source = "https://mirror.example.com/x.iso"
        calls = []

        def fake_check_url(url, *a, **k):
            if url == refused:
                return "refusing to fetch cloud metadata"
            return ""

        def fake_get(url, **kwargs):
            calls.append(url)
            return _RedirectResponse(302, location=refused)

        image = self._image()
        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", fake_check_url), \
             patch.object(fetcher.requests, "get", fake_get):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()

        # The refused address must never be requested -- only the source
        # URL (which redirects to it) was.
        self.assertEqual(calls, [source])
        self.assertNotIn(refused, calls)
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("refused", image.import_error.lower())
        self.assertEqual(self._leftovers(), [])

    def test_a_second_hop_redirect_to_a_refused_destination_is_caught(self):
        """Proves the per-hop check runs on every hop, not just the first --
        a guard that only re-checks once wouldn't catch a mirror that
        redirects somewhere ordinary before redirecting somewhere refused."""
        from . import fetcher

        first_hop = "https://mirror.example.com/x.iso"
        second_hop = "https://cdn.example.com/x.iso"
        refused = "http://169.254.169.254/x.iso"
        calls = []

        def fake_check_url(url, *a, **k):
            if url == refused:
                return "refusing to fetch cloud metadata"
            return ""

        def fake_get(url, **kwargs):
            calls.append(url)
            if url == first_hop:
                return _RedirectResponse(302, location=second_hop)
            if url == second_hop:
                return _RedirectResponse(302, location=refused)
            raise AssertionError(f"unexpected request to {url}")

        image = self._image()
        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", fake_check_url), \
             patch.object(fetcher.requests, "get", fake_get):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()

        self.assertEqual(calls, [first_hop, second_hop])
        self.assertNotIn(refused, calls)
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("refused", image.import_error.lower())
        self.assertEqual(self._leftovers(), [])

    def test_a_redirect_chain_longer_than_the_cap_is_refused(self):
        """An endless redirect chain must not hang a worker -- and must not
        be attempted forever trying to find out."""
        from . import fetcher

        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            # Always redirects to a new address; never terminates.
            return _RedirectResponse(
                302, location=f"https://mirror.example.com/hop{len(calls)}.iso")

        image = self._image()
        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", lambda *a, **k: ""), \
             patch.object(fetcher.requests, "get", fake_get):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()

        self.assertEqual(len(calls), fetcher._MAX_REDIRECTS + 1)
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("redirect", image.import_error.lower())
        self.assertEqual(self._leftovers(), [])

    def test_progress_counter_moves_during_a_multi_chunk_download(self):
        """Progress must be visible while the download is in flight, not only
        once it finishes — an operator watching the page needs it to move."""
        from . import fetcher

        image = self._image()
        seen = []
        parts = [PAYLOAD[:10], PAYLOAD[10:20], PAYLOAD[20:]]

        class _TrackingResponse(_FakeResponse):
            def iter_content(self, chunk_size):
                for chunk in parts:
                    yield chunk
                    # Runs when the fetcher's for-loop asks for the next
                    # chunk, i.e. strictly after it processed this one —
                    # a real mid-flight snapshot, not a post-hoc read.
                    seen.append(
                        OSImage.objects.get(pk=image.id).bytes_downloaded)

        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", lambda *a, **k: ""), \
             patch.object(fetcher.requests, "get",
                          lambda *a, **k: _TrackingResponse(parts)), \
             patch.object(fetcher, "import_iso", lambda *a, **k: None):
            fetcher.download_and_import(image.id)

        self.assertEqual(len(seen), 3)
        self.assertEqual(seen, sorted(seen))
        self.assertLess(seen[0], seen[-1])
        self.assertEqual(seen[-1], len(PAYLOAD))
