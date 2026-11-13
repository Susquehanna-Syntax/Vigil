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
        self.assertEqual(defang("[x][1]\n[1]: https://e.test"),
                         "[x]\u200b[1]\u200b\n[1]\u200b: https://e.test")
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


class DiscordTests(_Net, TestCase):
    HOOK = "https://discord.com/api/webhooks/1234/TOKENSECRET"

    def _send(self, **extra):
        channels.get("discord").send({"webhook_url": self.HOOK}, message(**extra))
        return json.loads(self.transport.call_args.args[6])

    def test_the_webhook_url_host_and_path_are_the_ones_the_admin_entered(self):
        channels.get("discord").send({"webhook_url": self.HOOK}, message())
        method, scheme, address, port, hostname, path, _body, _h = self.transport.call_args.args
        self.assertEqual((method, scheme, address, port, hostname, path),
                         ("POST", "https", PUBLIC, 443, "discord.com",
                          "/api/webhooks/1234/TOKENSECRET"))

    def test_mentions_are_switched_off_and_no_embed_has_a_color(self):
        sent = self._send()
        self.assertEqual(sent["allowed_mentions"], {"parse": []})
        self.assertNotIn("color", json.dumps(sent))

    def test_the_content_line_is_the_headline(self):
        self.assertEqual(self._send()["content"], "[FIRING] [CRITICAL] Alert: web-1")

    def test_every_fact_arrives_as_an_inline_embed_field(self):
        embed = self._send()["embeds"][0]
        self.assertEqual([(f["name"], f["value"]) for f in embed["fields"]],
                         [("Host", "web-1"), ("Site", "Leeds"), ("Value", "97.0"),
                          ("Rule", "High CPU")])
        self.assertTrue(all(f["inline"] for f in embed["fields"]))
        self.assertEqual(embed["description"], "CPU 97% > 90% <b>&")
        self.assertEqual(embed["url"], "https://vigil.test/hosts/7/")

    def test_blank_text_and_link_leave_their_keys_out_of_the_embed(self):
        embed = self._send(text="", link="")["embeds"][0]
        self.assertNotIn("description", embed)
        self.assertNotIn("url", embed)

    def test_a_long_title_arrives_cut_to_discords_limit(self):
        embed = self._send(title="t" * 300)["embeds"][0]
        self.assertEqual(embed["title"], "t" * 256)

    def test_no_alert_text_can_ping_a_room_or_hide_a_link(self):
        sent = self._send(text="see [login](https://evil.test) @Everyone", host="@everyone")
        body = json.dumps(sent, ensure_ascii=False)
        for raw in ("@everyone", "@Everyone", "](http"):
            self.assertNotIn(raw, body)
        self.assertIn("@\u200beveryone", body)

    def test_a_failure_is_a_channel_error(self):
        self.transport.return_value = 500
        with self.assertRaises(channels.ChannelError):
            channels.get("discord").send({"webhook_url": self.HOOK}, message())


class TeamsTests(_Net, TestCase):
    HOOK = ("https://prod-14.westus.logic.azure.com/workflows/"
            "9f1b7c0d2e3f4a5b8c9d0e1f2a3b4c5d?api-version=2016-06-01")

    def _send(self, **extra):
        channels.get("teams").send({"webhook_url": self.HOOK}, message(**extra))
        return json.loads(self.transport.call_args.args[6])

    def card(self, **extra):
        return self._send(**extra)["attachments"][0]["content"]

    def test_the_workflow_url_host_and_path_are_the_ones_the_admin_entered(self):
        channels.get("teams").send({"webhook_url": self.HOOK}, message())
        method, scheme, address, port, hostname, path, _body, _h = self.transport.call_args.args
        self.assertEqual((method, scheme, address, port, hostname),
                         ("POST", "https", PUBLIC, 443, "prod-14.westus.logic.azure.com"))
        self.assertEqual(path, "/workflows/9f1b7c0d2e3f4a5b8c9d0e1f2a3b4c5d"
                               "?api-version=2016-06-01")

    def test_the_attachment_is_an_adaptive_card(self):
        sent = self._send()
        attachment = sent["attachments"][0]
        self.assertEqual(sent["type"], "message")
        self.assertEqual(attachment["contentType"], "application/vnd.microsoft.card.adaptive")
        self.assertIsNone(attachment["contentUrl"])
        self.assertEqual(attachment["content"]["type"], "AdaptiveCard")
        self.assertEqual(self.card()["$schema"],
                         "http://adaptivecards.io/schemas/adaptive-card.json")
        self.assertNotIn("style", json.dumps(sent))

    def test_the_headline_is_the_first_textblock_and_every_fact_is_in_the_factset(self):
        card = self.card()
        self.assertEqual(card["body"][0],
                         {"type": "TextBlock", "text": "[FIRING] [CRITICAL] Alert: web-1",
                          "weight": "Bolder", "wrap": True})
        self.assertEqual(card["body"][1]["text"], "CPU 97% > 90% <b>&")
        self.assertEqual(card["body"][2]["facts"],
                         [{"title": "Host", "value": "web-1"},
                          {"title": "Site", "value": "Leeds"},
                          {"title": "Value", "value": "97.0"},
                          {"title": "Rule", "value": "High CPU"}])

    def test_the_link_is_the_open_url_action(self):
        card = self.card()
        self.assertEqual(card["version"], "1.4")
        self.assertEqual(card["actions"], [{"type": "Action.OpenUrl",
                                            "title": "Open in Vigil",
                                            "url": "https://vigil.test/hosts/7/"}])

    def test_blank_text_facts_and_link_leave_the_card_with_only_the_headline(self):
        card = self.card(text="", host="", site="", value=None, rule="", link="")
        self.assertEqual([b["type"] for b in card["body"]], ["TextBlock"])
        self.assertNotIn("actions", card)

    def test_no_alert_text_can_ping_a_room_or_hide_a_link(self):
        everything = json.dumps(self.card(text="see [x](https://e.test)", host="@everyone",
                                          title="Alert @everyone"), ensure_ascii=False)
        for raw in ("@everyone", "](http"):
            self.assertNotIn(raw, everything)
        self.assertIn("@\u200beveryone", everything)
        self.assertIn("[x]\u200b(https://e.test)", everything)

    def test_a_failure_is_a_channel_error(self):
        self.transport.return_value = 500
        with self.assertRaises(channels.ChannelError):
            channels.get("teams").send({"webhook_url": self.HOOK}, message())


class NtfyTests(_Net, TestCase):
    SERVER = "https://ntfy.test"

    def _sent(self, **extra):
        """Send through the channel and return what left for the server: the
        body as a dict, plus the headers."""
        config = {"server": self.SERVER, "topic": "vigil-ops"}
        config.update(extra.pop("config", {}))
        channels.get("ntfy").send(config, message(**extra))
        return json.loads(self.transport.call_args.args[6]), self.transport.call_args.args[7]

    def test_a_firing_critical_alert_posts_to_the_root_with_priority_5(self):
        sent, _headers = self._sent()
        self.assertEqual(sent["topic"], "vigil-ops")
        self.assertEqual(sent["title"], "[FIRING] [CRITICAL] Alert: web-1")
        self.assertEqual(sent["message"],
                         "CPU 97% > 90% <b>&\nHost: web-1\nSite: Leeds\nValue: 97.0\n"
                         "Rule: High CPU")
        self.assertEqual(sent["priority"], 5)
        self.assertEqual(sent["click"], "https://vigil.test/hosts/7/")
        method, scheme, address, port, hostname, path = self.transport.call_args.args[:6]
        self.assertEqual((method, scheme, address, port, hostname, path),
                         ("POST", "https", PUBLIC, 443, "ntfy.test", "/"))

    def test_a_resolved_alert_has_priority_3(self):
        sent, _headers = self._sent(event="resolved")
        self.assertEqual(sent["priority"], 3)

    def test_a_trailing_slash_does_not_double_the_path(self):
        self._sent(config={"server": "https://ntfy.example.test/"})
        self.assertEqual(self.transport.call_args.args[5], "/")

    def test_a_token_is_sent_as_a_bearer_header(self):
        _, headers = self._sent(config={"token": "tk_abc"})
        self.assertEqual(headers["Authorization"], "Bearer tk_abc")

    def test_without_a_token_there_is_no_authorization_header(self):
        _, headers = self._sent()
        self.assertNotIn("Authorization", headers)

    def test_ntfy_tags_are_never_sent(self):
        sent, _headers = self._sent()
        self.assertNotIn("tags", sent)

    def test_a_topic_with_stray_characters_is_refused_before_any_request(self):
        with self.assertRaises(channels.ChannelError):
            self._sent(config={"topic": "bad topic!"})
        self.transport.assert_not_called()

    def test_a_refusal_is_a_channel_error(self):
        self.transport.return_value = 500
        with self.assertRaises(channels.ChannelError):
            channels.get("ntfy").send({"server": self.SERVER, "topic": "vigil-ops"}, message())


class GotifyTests(_Net, TestCase):
    SERVER = "https://gotify.test"

    def _send(self, **extra):
        """Send through the channel and return what left for the server: the
        body as a dict, plus the headers."""
        config = {"server": self.SERVER, "token": "At_appsecret"}
        config.update(extra.pop("config", {}))
        channels.get("gotify").send(config, message(**extra))
        return json.loads(self.transport.call_args.args[6]), self.transport.call_args.args[7]

    def test_a_firing_critical_alert_posts_to_message_with_priority_8(self):
        sent, _headers = self._send()
        self.assertEqual(sent["title"], "[FIRING] [CRITICAL] Alert: web-1")
        self.assertEqual(sent["message"],
                         "CPU 97% > 90% <b>&\nHost: web-1\nSite: Leeds\nValue: 97.0\n"
                         "Rule: High CPU")
        self.assertEqual(sent["priority"], 8)
        self.assertEqual(sent["extras"], {"client::notification": {
            "click": {"url": "https://vigil.test/hosts/7/"}}})
        method, scheme, address, port, hostname, path = self.transport.call_args.args[:6]
        self.assertEqual((method, scheme, address, port, hostname, path),
                         ("POST", "https", PUBLIC, 443, "gotify.test", "/message"))

    def test_the_token_is_a_header_and_never_part_of_the_path(self):
        _sent, headers = self._send()
        self.assertEqual(headers["X-Gotify-Key"], "At_appsecret")
        self.assertNotIn("At_appsecret", self.transport.call_args.args[5])

    def test_a_resolved_alert_has_priority_2(self):
        sent, _headers = self._send(event="resolved")
        self.assertEqual(sent["priority"], 2)

    def test_a_server_behind_a_path_prefix_posts_to_that_prefix(self):
        self._send(config={"server": "https://h.test/gotify/"})
        self.assertEqual(self.transport.call_args.args[5], "/gotify/message")

    def test_without_a_link_there_is_no_extras_key(self):
        sent, _headers = self._send(link="")
        self.assertNotIn("extras", sent)

    def test_a_refusal_is_a_channel_error(self):
        self.transport.return_value = 500
        with self.assertRaises(channels.ChannelError):
            channels.get("gotify").send({"server": self.SERVER, "token": "At_appsecret"},
                                        message())
