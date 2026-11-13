"""The native channel types (sprint M7): what each one sends, and the
``secret_url`` field the chat types keep their webhook URL in."""
import json

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role

from . import channels
from .models import NotificationChannel
from .test_channels import PUBLIC, _Net, _user


def message(**extra):
    """A firing alert with every optional fact filled in."""
    base = dict(event="firing", severity="critical", title="Alert: web-1",
                text="CPU 97% > 90% <b>&", host="web-1", site="Leeds", value=97.0,
                rule="High CPU", link="https://vigil.test/hosts/7/")
    base.update(extra)
    return channels.Message(**base)


class SecretUrlTests(_Net, TestCase):
    """Slack stands in for every type whose URL is its credential."""

    HOOK = "https://hooks.slack.com/services/T000/B000/XXXXSECRET"

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(_user("admin", Role.ADMIN))

    def _create(self, url=HOOK):
        return self.client.post("/api/v1/alerts/channels/", {
            "name": "Ops chat", "kind": "slack", "config": {"webhook_url": url}}, format="json")

    def test_the_url_is_stored_encrypted_and_shown_only_as_scheme_and_host(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        row = resp.json()
        self.assertEqual(row["config"], {"webhook_url": "https://hooks.slack.com/..."})
        self.assertEqual(row["secrets_set"], ["webhook_url"])
        self.assertNotIn(b"XXXXSECRET", self.client.get("/api/v1/alerts/channels/").content)
        stored = NotificationChannel.objects.get(pk=row["id"]).config["webhook_url"]
        self.assertTrue(stored.startswith(channels.ENCRYPTED_PREFIX))

    def test_editing_without_the_url_keeps_it_and_a_new_one_replaces_it(self):
        row = self._create().json()
        url = f"/api/v1/alerts/channels/{row['id']}/"
        self.assertEqual(self.client.patch(url, {"name": "Renamed", "config": {}},
                                           format="json").status_code, 200)
        channel = NotificationChannel.objects.get(pk=row["id"])
        self.assertEqual(channels.plain_config(channels.get("slack"), channel.config),
                         {"webhook_url": self.HOOK})
        new = "https://hooks.slack.com/services/T1/B1/NEW"
        self.assertEqual(self.client.patch(url, {"config": {"webhook_url": new}},
                                           format="json").status_code, 200)
        channel.refresh_from_db()
        self.assertEqual(channels.plain_config(channels.get("slack"), channel.config)["webhook_url"],
                         new)

    def test_it_must_be_http_and_is_checked_as_an_address(self):
        resp = self._create("ftp://hooks.slack.com/x")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("http", resp.json()["config"]["webhook_url"])
        self.resolve.return_value = ["127.0.0.1"]
        resp = self._create()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("webhook_url", resp.json()["config"])

    def test_a_new_url_needs_the_other_secrets_again(self):
        """With a secret beside it, changing the URL drops that secret too."""
        kind = channels.ChannelType()
        kind.fields = (channels.Field("hook", "Hook", kind="secret_url"),
                       channels.Field("token", "Token", kind="secret"))
        stored, errors = channels.clean_config(kind, {"hook": "https://a.test/1", "token": "t"})
        self.assertEqual(errors, {})
        kept, errors = channels.clean_config(kind, {"hook": "https://a.test/1"}, stored)
        self.assertEqual(errors, {})
        self.assertEqual(kept["token"], stored["token"])
        _, errors = channels.clean_config(kind, {"hook": "https://b.test/2"}, stored)
        self.assertIn("enter it again", errors["token"])


class DefangTests(TestCase):
    def test_mass_mentions_and_markdown_links_are_broken_invisibly(self):
        from .channels.text import defang

        self.assertEqual(defang("@channel @HERE @all @everyone"),
                         "@\u200bchannel @\u200bHERE @\u200ball @\u200beveryone")
        self.assertEqual(defang("[x](https://e.test)"), "[x]\u200b(https://e.test)")
        self.assertEqual(defang("mail ops@example.com about @alice"),
                         "mail ops@example.com about @alice")


class SlackTests(_Net, TestCase):
    HOOK = "https://hooks.slack.com/services/T000/B000/XXXX"

    def test_the_body_escapes_markup_and_carries_every_fact(self):
        channels.get("slack").send({"webhook_url": self.HOOK}, message())
        _m, _s, address, _p, hostname, path, body, _h = self.transport.call_args.args
        self.assertEqual((address, hostname, path), (PUBLIC, "hooks.slack.com",
                                                     "/services/T000/B000/XXXX"))
        sent = json.loads(body)
        self.assertEqual(sent["text"], "[FIRING] [CRITICAL] Alert: web-1")
        text = json.dumps(sent["blocks"])
        self.assertIn("CPU 97% &gt; 90% &lt;b&gt;&amp;", text)
        self.assertNotIn("<b>", text)
        for fact in ("Host: web-1", "Site: Leeds", "Value: 97.0", "Rule: High CPU"):
            self.assertIn(fact, text)
        self.assertEqual(sent["blocks"][-1]["elements"][0]["url"], "https://vigil.test/hosts/7/")

    def test_no_alert_text_can_ping_a_room_or_hide_a_link(self):
        channels.get("slack").send({"webhook_url": self.HOOK}, message(
            title="Alert: <!channel> @here", text="see [login](https://evil.test) @Everyone",
            host="@all"))
        sent = json.loads(self.transport.call_args.args[6])
        everything = json.dumps(sent, ensure_ascii=False)
        self.assertNotIn("<!channel>", everything)
        self.assertIn("&lt;!channel&gt;", sent["text"])
        for raw in ("@here", "@Everyone", "@all", "](https"):
            self.assertNotIn(raw, everything)
        self.assertIn("@\u200bhere", sent["text"])

    def test_blank_facts_and_link_are_left_out(self):
        channels.get("slack").send({"webhook_url": self.HOOK},
                                   message(host="", site="", value=None, rule="", link=""))
        blocks = json.loads(self.transport.call_args.args[6])["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["section"])

    def test_a_refusal_is_a_channel_error(self):
        self.transport.return_value = 404
        with self.assertRaises(channels.ChannelError):
            channels.get("slack").send({"webhook_url": self.HOOK}, message())
