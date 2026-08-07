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

    def test_a_redirect_to_a_refused_destination_aborts_and_cleans_up(self):
        from . import fetcher

        image = self._image()

        def fake_check_url(url, *a, **k):
            if url == "https://169.254.169.254/x.iso":
                return "refusing to fetch cloud metadata"
            return ""

        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name), \
             patch.object(fetcher, "check_url", fake_check_url), \
             patch.object(fetcher.requests, "get",
                          lambda *a, **k: _FakeResponse(
                              [PAYLOAD], url="https://169.254.169.254/x.iso")):
            fetcher.download_and_import(image.id)
        image.refresh_from_db()
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("refused", image.import_error.lower())
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
