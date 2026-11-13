"""Notification channel types and the channels API (sprint M7)."""
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from . import channels
from .models import Alert, AlertRule, NotificationChannel
from .notifications import dispatch_alert_notification


def _user(name, role):
    user = get_user_model().objects.create_user(name, password="pw")
    UserProfile.objects.create(user=user, role=role)
    return user


PUBLIC = "93.184.216.34"


def network(status=200, address=PUBLIC):
    """Patch name resolution and the transport: every name resolves to
    *address*, and every request answers *status*. Returns the transport mock;
    its call args are (method, scheme, address, port, hostname, path, body,
    headers)."""
    resolve = mock.patch("apps.alerts.channels._resolve", return_value=[address])
    transport = mock.patch("apps.alerts.channels._transport", return_value=status)
    return resolve, transport


class _Net:
    """Mixin: a public network that answers 200, unless a test changes it."""

    def setUp(self):
        super().setUp()
        resolve, transport = network()
        self.resolve = resolve.start()
        self.transport = transport.start()
        self.addCleanup(mock.patch.stopall)

    def sent_json(self):
        import json
        return json.loads(self.transport.call_args.args[6])

    def sent_headers(self):
        return self.transport.call_args.args[7]


class CleanConfigTests(TestCase):
    def setUp(self):
        self.webhook = channels.get("webhook")
        self.email = channels.get("email")

    def test_a_required_field_and_a_url_are_checked(self):
        _, errors = channels.clean_config(self.webhook, {})
        self.assertEqual(errors, {"url": "required"})
        _, errors = channels.clean_config(self.webhook, {"url": "ftp://x"})
        self.assertIn("url", errors)

    def test_a_list_field_takes_lines_or_commas_and_drops_unknown_keys(self):
        config, errors = channels.clean_config(
            self.email, {"recipients": "a@x.test, b@x.test\nc@x.test", "junk": 1})
        self.assertEqual(errors, {})
        self.assertEqual(config, {"recipients": ["a@x.test", "b@x.test", "c@x.test"]})

    def test_a_secret_is_stored_encrypted_and_a_blank_one_keeps_the_old(self):
        config, _ = channels.clean_config(self.webhook, {"url": "https://h.test", "secret": "s3"})
        self.assertTrue(config["secret"].startswith(channels.ENCRYPTED_PREFIX))
        self.assertNotIn("s3", config["secret"])
        self.assertEqual(channels.plain_config(self.webhook, config)["secret"], "s3")
        kept, _ = channels.clean_config(self.webhook, {"url": "https://h.test", "secret": ""},
                                        existing=config)
        self.assertEqual(kept["secret"], config["secret"])

    def test_a_secret_stored_before_encryption_still_works(self):
        self.assertEqual(channels.plain_config(self.webhook, {"secret": "old"})["secret"], "old")


class WebhookSendTests(_Net, TestCase):
    def test_the_body_is_the_payload_unchanged_and_goes_to_the_checked_address(self):
        payload = {"event": "firing", "host": "h", "message": "hot"}
        message = channels.Message(event="firing", severity="warning", title="t",
                                   text="hot", payload=payload)
        channels.get("webhook").send({"url": "https://h.test:8443/hook?x=1", "secret": "s3"}, message)
        method, scheme, address, port, hostname, path, _body, headers = self.transport.call_args.args
        self.assertEqual((method, scheme, address, port, hostname, path),
                         ("POST", "https", PUBLIC, 8443, "h.test", "/hook?x=1"))
        self.assertEqual(self.sent_json(), payload)
        self.assertEqual(headers["X-Vigil-Secret"], "s3")
        self.assertEqual(headers["Host"], "h.test:8443")

    def test_a_failure_names_the_status_and_never_the_body(self):
        self.transport.return_value = 500
        message = channels.Message(event="test", severity="info", title="t", text="x")
        with self.assertRaises(channels.ChannelError) as ctx:
            channels.get("webhook").send({"url": "https://h.test"}, message)
        self.assertEqual(str(ctx.exception), "the server answered HTTP 500")


class DestinationTests(TestCase):
    """A channel cannot be turned into a way to make Vigil call itself, and
    only an admin of the whole install may point one at the LAN."""

    def check(self, address, private_ok=False):
        with mock.patch("apps.alerts.channels._resolve", return_value=[address]):
            return channels.check_destination("https://hook.test/x", private_ok=private_ok)

    def test_loopback_link_local_and_friends_are_always_refused(self):
        for address in ("127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "::ffff:127.0.0.1",
                        "224.0.0.1", "240.0.0.1"):
            with self.assertRaises(channels.ChannelError, msg=address):
                self.check(address, private_ok=True)

    def test_a_private_address_needs_private_ok(self):
        with self.assertRaises(channels.ChannelError):
            self.check("10.0.0.5")
        self.assertEqual(self.check("10.0.0.5", private_ok=True), "10.0.0.5")
        self.assertEqual(self.check(PUBLIC), PUBLIC)

    def test_every_resolved_address_is_checked(self):
        with mock.patch("apps.alerts.channels._resolve", return_value=[PUBLIC, "127.0.0.1"]):
            with self.assertRaises(channels.ChannelError):
                channels.check_destination("https://hook.test/x", private_ok=True)

    def test_a_url_seen_without_write_access_shows_only_scheme_and_host(self):
        shown, _ = channels.public_config(
            channels.get("webhook"), {"url": "https://hooks.chat.test/T000/B000/tok3n"}, full=False)
        self.assertEqual(shown, {"url": "https://hooks.chat.test/..."})


class ChannelApiTests(_Net, TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.admin = _user("admin", Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def _create(self, **extra):
        body = {"name": "Ops", "kind": "webhook",
                "config": {"url": "https://h.test/hook", "secret": "s3"}, **extra}
        return self.client.post("/api/v1/alerts/channels/", body, format="json")

    def test_the_types_list_carries_each_form(self):
        kinds = {t["kind"]: t for t in self.client.get("/api/v1/alerts/channels/types/").json()}
        self.assertIn("email", kinds)
        self.assertEqual([f["name"] for f in kinds["webhook"]["fields"]], ["url", "secret"])
        self.assertTrue(kinds["webhook"]["available"])

    def test_a_secret_is_never_returned(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.content)
        row = resp.json()
        self.assertEqual(row["config"], {"url": "https://h.test/hook"})
        self.assertEqual(row["secrets_set"], ["secret"])
        self.assertNotIn(b"s3", self.client.get("/api/v1/alerts/channels/").content)
        stored = NotificationChannel.objects.get(pk=row["id"]).config["secret"]
        self.assertTrue(stored.startswith("enc:"))

    def test_editing_without_the_secret_keeps_it(self):
        row = self._create().json()
        resp = self.client.patch(f"/api/v1/alerts/channels/{row['id']}/",
                                 {"name": "Ops 2", "config": {"url": "https://h.test/hook"}},
                                 format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        channel = NotificationChannel.objects.get(pk=row["id"])
        self.assertEqual(channels.plain_config(channels.get("webhook"), channel.config),
                         {"url": "https://h.test/hook", "secret": "s3"})

    def test_changing_the_address_needs_the_secret_again(self):
        row = self._create().json()
        url = f"/api/v1/alerts/channels/{row['id']}/"
        resp = self.client.patch(url, {"config": {"url": "https://elsewhere.test/"}}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("enter it again", resp.json()["config"]["secret"])
        resp = self.client.patch(url, {"config": {"url": "https://elsewhere.test/", "secret": "n3w"}},
                                 format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_an_instance_admin_may_point_a_channel_at_the_lan(self):
        self.resolve.return_value = ["10.0.0.5"]
        row = self._create(config={"url": "http://ntfy.lan/alerts"}).json()
        self.assertTrue(NotificationChannel.objects.get(pk=row["id"]).allow_private_targets)
        self.resolve.return_value = ["127.0.0.1"]
        resp = self._create(name="Self", config={"url": "http://myself.lan/"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("will not send to", resp.json()["config"]["url"])

    def test_bad_input_is_refused(self):
        self.assertEqual(self._create(kind="carrier-pigeon").status_code, 400)
        self.assertEqual(self._create(config={"url": "nope"}).status_code, 400)
        self.assertEqual(self._create().status_code, 201)
        self.assertEqual(self._create(name="ops").status_code, 400)  # names are unique
        row = NotificationChannel.objects.get(name="Ops")
        resp = self.client.patch(f"/api/v1/alerts/channels/{row.pk}/", {"kind": "email"},
                                 format="json")
        self.assertEqual(resp.status_code, 400)

    def test_a_viewer_gets_nothing(self):
        row = self._create().json()
        viewer = APIClient()
        viewer.force_authenticate(_user("viewer", Role.VIEWER))
        self.assertEqual(viewer.get("/api/v1/alerts/channels/").status_code, 403)
        self.assertEqual(viewer.post(f"/api/v1/alerts/channels/{row['id']}/test/").status_code, 403)

    def test_send_test_reports_the_outcome_and_records_it(self):
        row = self._create().json()
        self.transport.return_value = 404
        resp = self.client.post(f"/api/v1/alerts/channels/{row['id']}/test/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["detail"], "the server answered HTTP 404")
        self.assertIs(body["channel"]["last_ok"], False)
        self.transport.return_value = 200
        resp = self.client.post(f"/api/v1/alerts/channels/{row['id']}/test/")
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(self.sent_json()["event"], "test")

    def test_responses_match_the_api_schema(self):
        from vigil.schema_check import assert_shape

        from .channel_schema import (
            AlertChannelSerializer,
            AlertChannelTestSerializer,
            AlertChannelTypeSerializer,
        )

        row = self._create().json()
        assert_shape(self, row, AlertChannelSerializer)
        assert_shape(self, self.client.get("/api/v1/alerts/channels/").json(),
                     AlertChannelSerializer(many=True))
        assert_shape(self, self.client.get("/api/v1/alerts/channels/types/").json(),
                     AlertChannelTypeSerializer(many=True))
        body = self.client.post(f"/api/v1/alerts/channels/{row['id']}/test/").json()
        assert_shape(self, body, AlertChannelTestSerializer)

    def test_delete(self):
        row = self._create().json()
        self.assertEqual(self.client.delete(f"/api/v1/alerts/channels/{row['id']}/").status_code, 204)
        self.assertFalse(NotificationChannel.objects.exists())


class DeliveryTests(_Net, TestCase):
    def setUp(self):
        super().setUp()
        host = Host.objects.create(hostname="web-1", agent_token="t" * 32)
        rule = AlertRule.objects.create(name="cpu", category="cpu", metric="cpu_percent",
                                        operator="gt", threshold=90,
                                        severity=AlertRule.Severity.WARNING)
        self.alert = Alert.objects.create(host=host, rule=rule, severity="warning", message="hot")
        config, _ = channels.clean_config(channels.get("webhook"),
                                          {"url": "https://h.test", "secret": "s3"})
        self.ok = NotificationChannel.objects.create(name="ok", kind="webhook", config=config)
        self.ghost = NotificationChannel.objects.create(name="ghost", kind="retired-type")

    def test_each_channel_records_its_outcome_and_one_failure_spares_the_rest(self):
        dispatch_alert_notification(self.alert, "firing")
        self.assertEqual(self.sent_headers()["X-Vigil-Secret"], "s3")
        self.assertEqual(self.sent_json()["host"], "web-1")
        self.ok.refresh_from_db()
        self.ghost.refresh_from_db()
        self.assertIs(self.ok.last_ok, True)
        self.assertIs(self.ghost.last_ok, False)
        self.assertIn("unknown channel type", self.ghost.last_status)


    def test_a_channel_not_cleared_for_the_lan_cannot_reach_it_at_send_time(self):
        # Its name resolved publicly when saved; DNS now points it inside.
        self.resolve.return_value = ["192.168.1.10"]
        dispatch_alert_notification(self.alert, "firing")
        self.ok.refresh_from_db()
        self.assertIs(self.ok.last_ok, False)
        self.assertIn("private network", self.ok.last_status)
        self.transport.assert_not_called()


class EncryptMigrationTests(TestCase):
    def test_a_plaintext_webhook_secret_is_sealed(self):
        import importlib

        migration = importlib.import_module(
            "apps.alerts.migrations.0009_channel_types_and_last_status")
        channel = NotificationChannel.objects.create(
            name="old", kind="webhook", config={"url": "https://h.test", "secret": "plain"})
        migration.encrypt_webhook_secrets(django_apps, None)
        channel.refresh_from_db()
        self.assertTrue(channel.config["secret"].startswith("enc:"))
        self.assertEqual(channels.plain_config(channels.get("webhook"), channel.config)["secret"],
                         "plain")
