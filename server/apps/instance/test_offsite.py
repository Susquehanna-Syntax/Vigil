"""The sealed off-site copy of a backup (sprint M3 phase 4c).

Sealing: every way of altering a sealed file (a flipped bit, a dropped,
reordered or extra chunk, a cut) fails, and so does a wrong passphrase.
Signing: AWS's own Signature Version 4 example. Upload: never without a
passphrase, never plaintext, and pruning touches only our own copies.
"""
import io
import os
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from apps.instance import backup, offsite, sealing
from apps.instance.test_backup_task import _Dirs

PASS = "correct horse battery staple"


class SealingTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def roundtrip(self, data: bytes, passphrase=PASS):
        src, sealed, out = self.dir / "a", self.dir / "a.enc", self.dir / "a.out"
        src.write_bytes(data)
        sealing.seal(src, sealed, PASS)
        sealing.unseal(sealed, out, passphrase)
        return out.read_bytes(), sealed.read_bytes()

    def chunks(self, sealed: bytes):
        header, rest, out = sealed[:33], sealed[33:], []
        while rest:
            (n,) = struct.unpack(">I", rest[:4])
            out.append(rest[:4 + n])
            rest = rest[4 + n:]
        return header, out

    def refuses(self, blob: bytes):
        bad, out = self.dir / "bad.enc", self.dir / "bad.out"
        bad.write_bytes(blob)
        with self.assertRaises(sealing.SealError):
            sealing.unseal(bad, out, PASS)
        self.assertFalse(out.exists(), "a failed unseal must leave no plaintext behind")

    def test_roundtrip_at_every_chunk_boundary(self):
        for size in (0, 1, sealing.CHUNK - 1, sealing.CHUNK, sealing.CHUNK + 1, int(2.5 * sealing.CHUNK)):
            data = os.urandom(size)
            plain, sealed = self.roundtrip(data)
            self.assertEqual(plain, data, size)
            if size > 64:
                self.assertNotIn(data[:64], sealed)

    def test_a_wrong_passphrase_is_refused(self):
        src, sealed = self.dir / "a", self.dir / "a.enc"
        src.write_bytes(b"x" * 100)
        sealing.seal(src, sealed, PASS)
        out = self.dir / "o"
        with self.assertRaises(sealing.SealError):
            sealing.unseal(sealed, out, "wrong")
        self.assertFalse(out.exists())

    def test_every_alteration_is_refused(self):
        src, sealed = self.dir / "a", self.dir / "a.enc"
        src.write_bytes(os.urandom(int(2.5 * sealing.CHUNK)))
        sealing.seal(src, sealed, PASS)
        blob = sealed.read_bytes()
        header, chunks = self.chunks(blob)
        self.assertEqual(len(chunks), 3)
        flipped = bytearray(blob)
        flipped[len(header) + 100] ^= 1
        self.refuses(bytes(flipped))                                   # a flipped bit
        self.refuses(header + chunks[0] + chunks[1])                   # the last chunk dropped
        self.refuses(header + chunks[1] + chunks[0] + chunks[2])       # reordered
        self.refuses(header + b"".join(chunks) + chunks[2])            # one appended
        self.refuses(blob[:-10])                                       # cut short
        salt_flipped = bytearray(blob)
        salt_flipped[10] ^= 1
        self.refuses(bytes(salt_flipped))                              # header altered
        self.refuses(b"not a backup at all")

    def test_two_seals_of_the_same_file_differ(self):
        src = self.dir / "a"
        src.write_bytes(b"same")
        sealing.seal(src, self.dir / "1", PASS)
        sealing.seal(src, self.dir / "2", PASS)
        self.assertNotEqual((self.dir / "1").read_bytes(), (self.dir / "2").read_bytes())


class SigV4Tests(SimpleTestCase):
    def test_the_aws_documented_example(self):
        # docs.aws.amazon.com, "Create a signed AWS API request", IAM ListUsers.
        headers = offsite.sign(
            "GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
            region="us-east-1", service="iam", access_key="AKIDEXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", payload_hash=offsite.EMPTY_SHA256,
            headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
            now=datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc))
        self.assertTrue(headers["Authorization"].endswith(
            "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"))

    def test_a_bucket_needs_a_bare_endpoint_and_a_real_name(self):
        with self.assertRaises(offsite.OffsiteError):
            offsite.Bucket("https://s3.example/extra/path", "ok-bucket", "r", "a", "s")
        with self.assertRaises(offsite.OffsiteError):
            offsite.Bucket("https://s3.example", "Bad_Bucket", "r", "a", "s")


class _S3(_Dirs):
    def configure(self, **extra):
        values = {"VIGIL_BACKUP_S3_URL": "https://s3.example", "VIGIL_BACKUP_S3_BUCKET": "vigil-backups",
                  "VIGIL_BACKUP_S3_REGION": "us-east-1", "VIGIL_BACKUP_S3_ACCESS_KEY": "AK",
                  "VIGIL_BACKUP_S3_SECRET_KEY": "SK", "VIGIL_BACKUP_PASSPHRASE": PASS, **extra}
        from apps.instance import config
        real = config.setting
        p = mock.patch.object(config, "setting", side_effect=lambda n: values.get(n, real(n)))
        p.start()
        self.addCleanup(p.stop)

    def fake_s3(self, existing=()):
        self.uploaded, self.deleted = {}, []

        def put(url, data=None, headers=None, timeout=None):
            self.uploaded[url.rsplit("/", 1)[1]] = data.read()
            return mock.Mock(status_code=200, text="")

        def get(url, headers=None, timeout=None):
            keys = list(existing) + list(self.uploaded)
            body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            return mock.Mock(status_code=200, text=f"<ListBucketResult>{body}</ListBucketResult>")

        def delete(url, headers=None, timeout=None):
            self.deleted.append(url.rsplit("/", 1)[1])
            return mock.Mock(status_code=204, text="")

        for name, fn in (("put", put), ("get", get), ("delete", delete)):
            p = mock.patch.object(offsite.requests, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)


class OffsiteCopyTests(_S3):
    def test_nothing_is_uploaded_without_a_passphrase(self):
        self.configure(VIGIL_BACKUP_PASSPHRASE="")
        self.fake_s3()
        run = backup.run_backup()
        self.assertEqual((run.state, run.offsite), ("ok", "failed"))
        self.assertIn("never uploaded unencrypted", run.offsite_error)
        self.assertEqual(self.uploaded, {})

    def test_the_copy_is_sealed_and_never_plaintext(self):
        self.configure()
        self.fake_s3()
        run = backup.run_backup()
        self.assertEqual(run.offsite, "copied", run.offsite_error)
        [(key, body)] = self.uploaded.items()
        self.assertEqual(key, run.name + ".enc")
        self.assertTrue(body.startswith(sealing.MAGIC))
        self.assertNotIn(b"old-seed", body)
        self.assertNotIn(b"pretend database", body)
        self.assertFalse([p for p in self.out.iterdir() if p.name.endswith(".partial")])

    def test_old_copies_are_pruned_and_foreign_keys_left(self):
        self.configure()
        old = [f"vigil-2026010{d}T020000Z.tar.gz.enc" for d in range(1, 6)]
        self.fake_s3(existing=old + ["vigil-notes.txt", "someone-else.tar.gz.enc"])
        with self.settings_(keep=2):
            backup.run_backup()
        self.assertEqual(sorted(self.deleted), sorted(old[:4]))

    def test_an_upload_failure_leaves_the_local_backup_good(self):
        self.configure()
        self.fake_s3()
        with mock.patch.object(offsite.requests, "put", return_value=mock.Mock(status_code=403, text="denied")):
            run = backup.run_backup()
        self.assertEqual((run.state, run.offsite), ("ok", "failed"))
        self.assertIn("403", run.offsite_error)
        self.assertTrue((self.out / run.name).exists())


class SealedRestoreTests(_S3):
    def sealed_archive(self):
        run = backup.run_backup()
        sealed = self.out / (run.name + ".enc")
        sealing.seal(self.out / run.name, sealed, PASS)
        return sealed

    def restore(self, path, *extra):
        with mock.patch("apps.instance.management.commands.restore_backup.Command._restore_database"):
            call_command("restore_backup", str(path), "--yes", *extra, stdout=io.StringIO())

    def test_restores_from_a_sealed_copy_with_the_passphrase_file(self):
        sealed = self.sealed_archive()
        (self.secrets / "signing_key_seed").write_text("new-seed\n")
        pf = self.out / "pass.txt"
        pf.write_text(PASS + "\n")
        self.restore(sealed, "--passphrase-file", str(pf))
        self.assertEqual((self.secrets / "signing_key_seed").read_text().strip(), "old-seed")

    def test_a_wrong_or_missing_passphrase_is_refused(self):
        sealed = self.sealed_archive()
        pf = self.out / "pass.txt"
        pf.write_text("wrong\n")
        with self.assertRaisesMessage(CommandError, "Cannot decrypt"):
            self.restore(sealed, "--passphrase-file", str(pf))
        with mock.patch.dict(os.environ, {"VIGIL_BACKUP_PASSPHRASE": ""}), \
                self.assertRaisesMessage(CommandError, "passphrase"):
            self.restore(sealed)
