"""The configuration gate: it must catch a bad deploy and never a good one."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from vigil.startup_checks import (INSECURE_SECRET_KEYS, collect_problems,
                                  validate_or_die)

_GOOD_ENV = {
    "VIGIL_SIGNING_KEY_SEED": "sL/+T7Bu4Kq/6eJgvFyEIb+vnqxmDGHK5UHj0yekqC8=",
    "VIGIL_PUBLIC_URL": "https://vigil.example.com",
}


class ConfigurationGateTests(SimpleTestCase):

    @override_settings(DEBUG=False, SECRET_KEY="a-real-random-key")
    def test_a_complete_configuration_has_no_problems(self):
        with patch.dict("os.environ", _GOOD_ENV, clear=True):
            self.assertEqual(collect_problems(), [])

    @override_settings(DEBUG=False, SECRET_KEY="insecure-change-me")
    def test_the_compose_default_secret_key_is_caught(self):
        """The dangerous one: it works perfectly, so nothing else complains."""
        with patch.dict("os.environ", _GOOD_ENV, clear=True):
            problems = collect_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("DJANGO_SECRET_KEY", problems[0])

    @override_settings(DEBUG=False, SECRET_KEY="a-real-random-key")
    def test_every_insecure_default_is_caught(self):
        for key in INSECURE_SECRET_KEYS:
            with self.subTest(key=key), override_settings(SECRET_KEY=key):
                with patch.dict("os.environ", _GOOD_ENV, clear=True):
                    self.assertTrue(any("DJANGO_SECRET_KEY" in p
                                        for p in collect_problems()))

    @override_settings(DEBUG=False, SECRET_KEY="a-real-random-key")
    def test_a_missing_signing_seed_is_caught(self):
        env = {k: v for k, v in _GOOD_ENV.items() if k != "VIGIL_SIGNING_KEY_SEED"}
        with patch.dict("os.environ", env, clear=True):
            problems = collect_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("VIGIL_SIGNING_KEY_SEED", problems[0])

    @override_settings(DEBUG=False, SECRET_KEY="a-real-random-key")
    def test_no_reachable_address_is_caught(self):
        """The 400 nobody could diagnose: neither knob set."""
        env = {k: v for k, v in _GOOD_ENV.items() if k != "VIGIL_PUBLIC_URL"}
        with patch.dict("os.environ", env, clear=True):
            problems = collect_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("VIGIL_PUBLIC_URL", problems[0])

    @override_settings(DEBUG=False, SECRET_KEY="a-real-random-key")
    def test_either_address_knob_satisfies_it(self):
        env = {"VIGIL_SIGNING_KEY_SEED": _GOOD_ENV["VIGIL_SIGNING_KEY_SEED"],
               "DJANGO_ALLOWED_HOSTS": "vigil.example.com"}
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(collect_problems(), [])

    @override_settings(DEBUG=False, SECRET_KEY="insecure-change-me")
    def test_every_problem_is_reported_at_once(self):
        """The point of the whole module: an operator fixing their compose
        file should get the list, not the first item of it."""
        with patch.dict("os.environ", {}, clear=True):
            problems = collect_problems()
        self.assertEqual(len(problems), 3)

        with patch.dict("os.environ", {}, clear=True), \
                patch("vigil.startup_checks._is_non_serving_command",
                      return_value=False):
            with self.assertRaises(Exception) as caught:
                validate_or_die()
        message = str(caught.exception)
        for expected in ("DJANGO_SECRET_KEY", "VIGIL_SIGNING_KEY_SEED",
                         "VIGIL_PUBLIC_URL"):
            self.assertIn(expected, message)

    @override_settings(DEBUG=True, SECRET_KEY="insecure-change-me")
    def test_debug_says_this_is_not_a_production_instance(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(collect_problems(), [])

    @override_settings(DEBUG=False, SECRET_KEY="insecure-change-me")
    def test_a_non_serving_command_is_not_gated(self):
        """manage.py check is this project's build gate and must not become a
        configuration test — but gunicorn, which serves, still is."""
        with patch.dict("os.environ", {}, clear=True), \
                patch("sys.argv", ["manage.py", "check"]):
            validate_or_die()          # must not raise

    @override_settings(DEBUG=False, SECRET_KEY="a-real-random-key")
    def test_celery_needs_no_public_address_but_still_needs_its_secrets(self):
        """The compose file gives the Celery services no public URL; checking
        it there crash-looped both on every install from an empty .env."""
        env = {"VIGIL_SIGNING_KEY_SEED": _GOOD_ENV["VIGIL_SIGNING_KEY_SEED"]}
        for argv in (["/usr/local/bin/celery", "-A", "vigil", "worker"],
                     ["/usr/local/lib/python3.12/site-packages/celery/__main__.py", "beat"]):
            with self.subTest(argv=argv[0]), patch("sys.argv", argv), \
                    patch.dict("os.environ", env, clear=True):
                self.assertEqual(collect_problems(), [])
        with patch("sys.argv", ["/usr/local/bin/celery", "worker"]), \
                patch.dict("os.environ", {}, clear=True):
            self.assertTrue(any("VIGIL_SIGNING_KEY_SEED" in p for p in collect_problems()))
        with patch("sys.argv", ["/usr/local/bin/gunicorn", "vigil.wsgi"]), \
                patch.dict("os.environ", env, clear=True):
            self.assertTrue(any("VIGIL_PUBLIC_URL" in p for p in collect_problems()))
