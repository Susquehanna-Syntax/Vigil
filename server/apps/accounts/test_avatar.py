"""Your profile picture: only a real, small PNG, JPEG or WebP is kept."""

import base64

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()


class AvatarTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("pic", password="pw")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = "/api/v1/accounts/me/avatar/"

    def test_a_png_is_kept_and_removed(self):
        r = self.client.put(self.url, {"avatar": f"data:image/png;base64,{PNG}"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(self.client.get(self.url).json()["avatar"].startswith("data:image/png;base64,"))
        self.client.delete(self.url)
        self.assertEqual(self.client.get(self.url).json()["avatar"], "")

    def test_a_file_that_lies_about_its_type_is_refused(self):
        svg = base64.b64encode(b"<svg onload=alert(1)>").decode()
        for uri in (f"data:image/png;base64,{svg}", f"data:image/svg+xml;base64,{svg}",
                    "javascript:alert(1)", ""):
            with self.subTest(uri=uri[:30]):
                self.assertEqual(self.client.put(self.url, {"avatar": uri}, format="json").status_code, 400)

    def test_a_large_image_is_refused(self):
        big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (300 * 1024)).decode()
        r = self.client.put(self.url, {"avatar": f"data:image/png;base64,{big}"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_the_sidebar_shows_it(self):
        self.client.put(self.url, {"avatar": f"data:image/png;base64,{PNG}"}, format="json")
        self.client.force_login(self.user)
        page = self.client.get("/").content.decode()
        self.assertIn('class="sidebar-avatar"', page)
        self.assertIn("<img src=\"data:image/png;base64,", page)
