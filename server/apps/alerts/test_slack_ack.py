"""The Acknowledge button on a Slack alert (sprint M7): the interactivity
endpoint that checks Slack's signature and acks, and the button itself."""
import hashlib
import hmac
import json
import time
from unittest import mock
from urllib.parse import urlencode

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role
from apps.hosts.models import Host

from . import channels, slack_actions
from .models import Alert, NotificationChannel
from .test_channels import _Net, _user

URL = "/api/v1/alerts/slack/actions/"


def signature(secret: str, ts: str, body: bytes) -> str:
    """What Slack sends: HMAC-SHA256 over "v0:<timestamp>:<raw body>"."""
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body,
                            hashlib.sha256).hexdigest()


class _Clicks(_Net):
    """A Slack channel with a signing secret, and clicks signed for it."""

    HOOK = "https://hooks.slack.com/services/T000/B000/XXXX"
    RESPONSE_URL = "https://hooks.slack.com/services/T000/B000/response"
    SECRET = "shh"

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(_user("admin", Role.ADMIN))
        created = self.client.post("/api/v1/alerts/channels/", {
            "name": "Ops chat", "kind": "slack",
            "config": {"webhook_url": self.HOOK, "signing_secret": self.SECRET},
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.channel = NotificationChannel.objects.get(pk=created.json()["id"])
        posted = mock.patch("apps.alerts.slack_actions.channels.post")
        self.posted = posted.start()
        self.addCleanup(mock.patch.stopall)

    def _host(self, name="web-1"):
        return Host.objects.create(hostname=name, agent_token=f"tok-{name}" + "a" * 20,
                                   os="linux", status=Host.Status.ONLINE,
                                   mode=Host.Mode.MONITOR)

    def _alert(self, host, **extra):
        return Alert.objects.create(host=host, rule=None,
                                    state=extra.pop("state", Alert.State.FIRING),
                                    severity="warning", message="CPU 97% > 90%", **extra)

    def _value(self, *alerts):
        return ",".join(str(a.id) for a in alerts)

    def _payload(self, value, response_url=None, username="dana"):
        return {"token": "verification-token", "user": {"id": "U1", "username": username},
                "actions": [{"action_id": slack_actions.ACK_ACTION, "value": value}],
                "response_url": self.RESPONSE_URL if response_url is None else response_url}

    def _click(self, payload, secret=SECRET, ts=None):
        body = urlencode({"payload": json.dumps(payload)}).encode()
        stamp = str(int(time.time())) if ts is None else ts
        return self.client.post(URL, body,
                                content_type="application/x-www-form-urlencoded",
                                HTTP_X_SLACK_REQUEST_TIMESTAMP=stamp,
                                HTTP_X_SLACK_SIGNATURE=signature(secret, stamp, body))


class AcknowledgeClickTests(_Clicks, TestCase):
    def test_a_signed_click_acknowledges_the_alert_and_answers_in_slack(self):
        host = self._host()
        alert = self._alert(host)
        resp = self._click(self._payload(str(alert.id)))
        self.assertEqual(resp.status_code, 200, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.ACKNOWLEDGED)
        self.assertIsNotNone(alert.acknowledged_at)
        self.assertIsNone(alert.acknowledged_until)
        _url, kwargs = self.posted.call_args
        self.assertEqual(self.posted.call_args.args[0], self.RESPONSE_URL)
        self.assertEqual(kwargs["json"]["text"], "Acknowledged in Vigil by dana")
        self.assertFalse(kwargs["json"]["replace_original"])

    def test_every_id_in_the_button_value_is_acknowledged(self):
        host = self._host()
        alerts = [self._alert(host) for _ in range(3)]
        resp = self._click(self._payload(self._value(*alerts)))
        self.assertEqual(resp.status_code, 200, resp.content)
        for alert in alerts:
            alert.refresh_from_db()
            self.assertEqual(alert.state, Alert.State.ACKNOWLEDGED)
        text = self.posted.call_args.kwargs["json"]["text"]
        self.assertTrue(text.endswith("(3 alerts)"), text)

    def test_a_wrong_signature_acknowledges_nothing(self):
        alert = self._alert(self._host())
        resp = self._click(self._payload(str(alert.id)), secret="not-the-secret")
        self.assertEqual(resp.status_code, 401, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.posted.assert_not_called()

    def test_a_timestamp_ten_minutes_old_is_refused_even_when_signed(self):
        alert = self._alert(self._host())
        old = str(int(time.time()) - 600)
        resp = self._click(self._payload(str(alert.id)), ts=old)
        self.assertEqual(resp.status_code, 401, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.posted.assert_not_called()

    def test_a_channel_without_a_signing_secret_never_matches(self):
        # Its webhook URL does not even decrypt: that channel is skipped, and
        # the click is still checked against the others.
        NotificationChannel.objects.create(
            name="No secret", kind="slack",
            config={"webhook_url": "enc:throwaway"})
        alert = self._alert(self._host())
        resp = self._click(self._payload(str(alert.id)), secret="anything")
        self.assertEqual(resp.status_code, 401, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)

    def test_an_alert_someone_already_handled_is_left_alone(self):
        host = self._host()
        done = self._alert(host, state=Alert.State.ACKNOWLEDGED)
        gone = self._alert(host, state=Alert.State.RESOLVED)
        resp = self._click(self._payload(f"{done.id},{gone.id}"))
        self.assertEqual(resp.status_code, 200, resp.content)
        done.refresh_from_db()
        gone.refresh_from_db()
        self.assertEqual(done.state, Alert.State.ACKNOWLEDGED)
        self.assertEqual(gone.state, Alert.State.RESOLVED)
        self.assertEqual(self.posted.call_args.kwargs["json"]["text"],
                         "Nothing to acknowledge: already acknowledged or resolved")

    def test_a_response_url_on_another_host_posts_nothing(self):
        alert = self._alert(self._host())
        resp = self._click(self._payload(str(alert.id),
                                         response_url="https://evil.example/x"))
        self.assertEqual(resp.status_code, 200, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.ACKNOWLEDGED)
        self.posted.assert_not_called()

    def test_a_host_the_channel_may_not_hear_about_is_not_acknowledged(self):
        alert = self._alert(self._host())
        with mock.patch("apps.alerts.slack_actions.scoping.within_site", return_value=False):
            resp = self._click(self._payload(str(alert.id)))
        self.assertEqual(resp.status_code, 200, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.assertEqual(self.posted.call_args.kwargs["json"]["text"],
                         "Nothing to acknowledge: already acknowledged or resolved")

    def test_a_body_without_a_payload_is_a_400(self):
        stamp = str(int(time.time()))
        body = b"nope"
        resp = self.client.post(URL, body, content_type="application/x-www-form-urlencoded",
                                HTTP_X_SLACK_REQUEST_TIMESTAMP=stamp,
                                HTTP_X_SLACK_SIGNATURE=signature(self.SECRET, stamp, body))
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_a_channel_whose_secrets_do_not_decrypt_does_not_block_the_rest(self):
        NotificationChannel.objects.create(
            name="Broken", kind="slack",
            config={"webhook_url": "enc:throwaway", "signing_secret": "enc:throwaway"})
        alert = self._alert(self._host())
        resp = self._click(self._payload(str(alert.id)))
        self.assertEqual(resp.status_code, 200, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.ACKNOWLEDGED)

    def test_an_unsigned_body_is_a_401_whatever_it_holds(self):
        # The signature is checked before the body is read, so a caller without
        # the secret cannot tell a malformed click from a well-formed one.
        stamp = str(int(time.time()))
        resp = self.client.post(URL, b"nope", content_type="application/x-www-form-urlencoded",
                                HTTP_X_SLACK_REQUEST_TIMESTAMP=stamp,
                                HTTP_X_SLACK_SIGNATURE="v0=" + "0" * 64)
        self.assertEqual(resp.status_code, 401, resp.content)

    def test_a_disabled_channels_secret_no_longer_signs(self):
        NotificationChannel.objects.filter(pk=self.channel.pk).update(enabled=False)
        alert = self._alert(self._host())
        resp = self._click(self._payload(str(alert.id)))
        self.assertEqual(resp.status_code, 401, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)

    def test_a_click_on_another_button_is_acked_with_a_plain_200(self):
        alert = self._alert(self._host())
        payload = self._payload(str(alert.id))
        payload["actions"] = [{"action_id": "something_else", "value": "x"}]
        resp = self._click(payload)
        self.assertEqual(resp.status_code, 200, resp.content)
        alert.refresh_from_db()
        self.assertEqual(alert.state, Alert.State.FIRING)
        self.posted.assert_not_called()


class SlackButtonTests(_Net, TestCase):
    HOOK = "https://hooks.slack.com/services/T000/B000/XXXX"

    def _send(self, config=None, **message):
        base = {"event": "firing", "severity": "warning", "title": "Alert: web-1",
                "text": "CPU 97%"}
        base.update(message)
        channels.get("slack").send({"webhook_url": self.HOOK, **(config or {})},
                                   channels.Message(**base))
        return json.loads(self.transport.call_args.args[6])["blocks"]

    def _ack(self, blocks):
        for block in blocks:
            if block["type"] == "actions":
                for element in block["elements"]:
                    if element.get("action_id") == channels.slack.ACK_ACTION:
                        return element
        return None

    def test_a_firing_alert_with_a_signing_secret_gets_the_button_first(self):
        blocks = self._send(config={"signing_secret": "shh"},
                            payload={"alert_id": "11111111-1111-1111-1111-111111111111"})
        ack = self._ack(blocks)
        self.assertIsNotNone(ack)
        self.assertEqual(ack["value"], "11111111-1111-1111-1111-111111111111")
        actions = [b for b in blocks if b["type"] == "actions"][-1]
        self.assertEqual(actions["elements"][0]["action_id"], channels.slack.ACK_ACTION)

    def test_the_button_is_first_before_open_in_vigil(self):
        blocks = self._send(config={"signing_secret": "shh"}, link="https://vigil.test/hosts/7/",
                            payload={"alert_id": "a"})
        actions = [b for b in blocks if b["type"] == "actions"][-1]
        self.assertEqual([e.get("action_id") or e.get("url") for e in actions["elements"]],
                         [channels.slack.ACK_ACTION, "https://vigil.test/hosts/7/"])

    def test_a_grouped_notice_acks_up_to_fifty_of_its_alerts(self):
        ids = [f"{n:08d}-1111-1111-1111-111111111111" for n in range(60)]
        blocks = self._send(config={"signing_secret": "shh"}, payload={"alert_ids": ids})
        values = self._ack(blocks)["value"].split(",")
        self.assertEqual(len(values), 50)
        self.assertEqual(values[0], ids[0])

    def test_without_a_signing_secret_there_is_no_button(self):
        blocks = self._send(payload={"alert_id": "a"})
        self.assertIsNone(self._ack(blocks))

    def test_a_resolved_message_gets_no_button(self):
        blocks = self._send(config={"signing_secret": "shh"}, event="resolved",
                            payload={"alert_id": "a"})
        self.assertIsNone(self._ack(blocks))

    def test_a_message_naming_no_alerts_gets_no_button(self):
        blocks = self._send(config={"signing_secret": "shh"}, payload={})
        self.assertIsNone(self._ack(blocks))
