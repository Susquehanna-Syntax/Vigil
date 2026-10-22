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
