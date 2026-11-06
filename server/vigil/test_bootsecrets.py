"""First-boot secret generation. No database, no Django settings needed."""

import base64
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from vigil import bootsecrets


class BootSecretsTests(unittest.TestCase):

    def _clean_env(self, extra=None):
        """Patch os.environ with the three secret variables removed."""
        patcher = mock.patch.dict(os.environ, extra or {}, clear=False)
        env = patcher.start()
        self.addCleanup(patcher.stop)
        for name in bootsecrets.NAMES.values():
            env.pop(name, None)
        env.pop("VIGIL_SECRETS_DIR", None)
        return env

    @staticmethod
    def _read(directory, name):
        with open(os.path.join(directory, name)) as handle:
            return handle.read()

    def _mode(self, path):
        return oct(os.stat(path).st_mode & 0o777)

    def test_first_boot_generates_all_three(self):
        env = self._clean_env()
        # Unset would resolve to DEFAULT_DIR, which does not exist here.
        env["VIGIL_SECRETS_DIR"] = ""
        with tempfile.TemporaryDirectory() as d:
            outcomes = bootsecrets.ensure(d)
            self.assertEqual(
                set(outcomes.values()), {"generated"},
                msg=f"every secret must be generated on first boot, got {outcomes}")
            seed = self._read(d, "signing_key_seed").strip()
            self.assertEqual(len(seed), 44, msg="the seed must be 44 characters")
            self.assertEqual(len(base64.b64decode(seed)), 32,
                             msg="the seed must decode to 32 bytes")
            self.assertEqual(self._mode(os.path.join(d, "django_secret_key")), "0o600")
            self.assertEqual(self._mode(os.path.join(d, "signing_key_seed")), "0o600")
            self.assertEqual(self._mode(os.path.join(d, "postgres_password")), "0o644")
            self.assertEqual(self._mode(d), "0o711")

    def test_second_boot_keeps_them(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as d:
            bootsecrets.ensure(d)
            before = {name: self._read(d, name) for name in bootsecrets.NAMES}
            outcomes = bootsecrets.ensure(d)
            self.assertEqual(set(outcomes.values()), {"kept"},
                             msg=f"a second boot must not regenerate, got {outcomes}")
            after = {name: self._read(d, name) for name in bootsecrets.NAMES}
            self.assertEqual(before, after, msg="secret files must not change")

    def test_an_env_value_is_adopted_and_wins(self):
        seed = base64.b64encode(b"0" * 32).decode()
        env = self._clean_env()
        env["VIGIL_SIGNING_KEY_SEED"] = seed
        with tempfile.TemporaryDirectory() as d:
            outcomes = bootsecrets.ensure(d)
            self.assertEqual(outcomes["signing_key_seed"], "adopted")
            self.assertEqual(self._read(d, "signing_key_seed").strip(), seed)

            env["VIGIL_SECRETS_DIR"] = d
            self.assertEqual(bootsecrets.value("signing_key_seed"), seed)

            different = base64.b64encode(b"1" * 32).decode()
            env["VIGIL_SIGNING_KEY_SEED"] = different
            self.assertEqual(bootsecrets.value("signing_key_seed"), different,
                             msg="the environment always wins over the file")
            outcomes = bootsecrets.ensure(d)
            self.assertEqual(outcomes["signing_key_seed"], "kept")
            self.assertEqual(self._read(d, "signing_key_seed").strip(), seed,
                             msg="an existing secret file is never overwritten")

    def test_value_reads_the_file_when_env_is_blank(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "django_secret_key"), "w") as handle:
                handle.write("  from-the-file  \n")
            env = self._clean_env({"DJANGO_SECRET_KEY": "  "})
            env["VIGIL_SECRETS_DIR"] = d
            self.assertEqual(bootsecrets.value("django_secret_key"),
                             "from-the-file")
            self.assertEqual(bootsecrets.value("signing_key_seed", "fallback"),
                             "fallback")

    def test_an_existing_database_keeps_the_old_password(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as pg:
            with open(os.path.join(pg, "PG_VERSION"), "w") as handle:
                handle.write("16\n")
            outcomes = bootsecrets.ensure(d, pgdata=pg)
            self.assertEqual(outcomes["postgres_password"], "legacy")
            self.assertEqual(self._read(d, "postgres_password").strip(),
                             bootsecrets.LEGACY_POSTGRES_PASSWORD)
            self.assertEqual(outcomes["django_secret_key"], "generated")
            self.assertEqual(outcomes["signing_key_seed"], "generated")

    def test_a_new_database_gets_a_generated_password(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as pg:
            outcomes = bootsecrets.ensure(d, pgdata=pg)
            self.assertEqual(outcomes["postgres_password"], "generated")
            self.assertNotEqual(self._read(d, "postgres_password").strip(),
                                bootsecrets.LEGACY_POSTGRES_PASSWORD)

    def test_secrets_are_never_printed(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as d:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                bootsecrets.ensure(d)
            log = buffer.getvalue()
            for name in bootsecrets.NAMES:
                secret = self._read(d, name).strip()
                self.assertNotIn(secret, log,
                                 msg=f"{name} was printed to stdout")

    def test_a_lost_race_keeps_the_winners_file(self):
        self._clean_env()
        with tempfile.TemporaryDirectory() as d, mock.patch("os.open", side_effect=FileExistsError):
            bootsecrets.ensure(d)

    def test_unreadable_file_counts_as_missing(self):
        self._clean_env()
        with mock.patch("builtins.open", side_effect=PermissionError):
            self.assertEqual(bootsecrets.value("django_secret_key", "fallback"),
                             "fallback")


if __name__ == "__main__":
    unittest.main()
