"""The cache is shared between web workers whenever a deployment has Redis.

Rebuild enrolment tokens and security-key confirmations are stored by one
request and read by another, which may land on a different worker. With the
default in-process cache they would be found only by luck.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from vigil import settings as vigil_settings


class CacheUrlTests(SimpleTestCase):
    def test_the_brokers_redis_on_its_own_database(self):
        with patch.dict("os.environ", {"CELERY_BROKER_URL": "redis://redis:6379/0"}), \
                patch.dict("os.environ", {"VIGIL_CACHE_URL": ""}):
            self.assertEqual(vigil_settings._cache_url(), "redis://redis:6379/1")

    def test_an_explicit_cache_url_wins(self):
        with patch.dict("os.environ", {"CELERY_BROKER_URL": "redis://redis:6379/0",
                                       "VIGIL_CACHE_URL": "redis://cache:6379/5"}):
            self.assertEqual(vigil_settings._cache_url(), "redis://cache:6379/5")

    def test_no_redis_means_the_in_process_cache(self):
        with patch.dict("os.environ", {"CELERY_BROKER_URL": "", "VIGIL_CACHE_URL": ""}):
            self.assertEqual(vigil_settings._cache_url(), "")


class JSONSerializerTests(SimpleTestCase):
    def test_values_round_trip_as_data(self):
        from vigil.cache_json import JSONSerializer

        s = JSONSerializer()
        for value in (7, "wa_token", {"a": [1, "b"]}, ["x", None, True]):
            self.assertEqual(s.loads(s.dumps(value)), value)

    def test_a_pickle_is_never_executed(self):
        import pickle

        from vigil.cache_json import JSONSerializer

        with self.assertRaises(ValueError):
            JSONSerializer().loads(pickle.dumps({"x": 1}))

    def test_the_redis_cache_is_configured_with_it(self):
        src = (vigil_settings.BASE_DIR / "vigil" / "settings.py").read_text()
        self.assertIn('"serializer": "vigil.cache_json.JSONSerializer"', src)
