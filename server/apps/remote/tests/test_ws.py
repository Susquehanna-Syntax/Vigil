"""The two WebSockets, driven as their peers: a fake browser and a fake agent.

Every close code here is a refusal with a reason, so each one gets a test: a
wrong Origin, no login, someone else's session, a second browser, an agent with
another host's token, a closed session, and an agent coming direct while the
built-in relay is off.
"""

import base64
import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from apps.relays.models import Relay
from apps.relays.trust import link_token

from .. import bus, ws
from ..models import RemoteSession
from .asgi_harness import connect

HOST_TOKEN = "h" * 64
OTHER_TOKEN = "o" * 64


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@override_settings(VIGIL_SESSION_BUS="memory")
class _SocketCase(TransactionTestCase):
    def setUp(self):
        bus.reset()
        self.host = Host.objects.create(
            hostname="target", agent_token=HOST_TOKEN, status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED, agent_allow_remote_shell=True)
        self.other_host = Host.objects.create(
            hostname="bystander", agent_token=OTHER_TOKEN, status=Host.Status.ONLINE,
            mode=Host.Mode.MANAGED)
        self.user = self._user("alice")
        self.session = RemoteSession.objects.create(
            host=self.host, user=self.user, username="alice", kind="shell", nonce="n1")

    def _user(self, name):
        user = get_user_model().objects.create_user(username=name, password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        return user

    def cookie_for(self, user) -> str:
        self.client.force_login(user)
        value = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        return f"{settings.SESSION_COOKIE_NAME}={value}"

    async def browser(self, cookie=None, origin="http://testserver", sid=None):
        if cookie is None:
            cookie = await _get(lambda: self.cookie_for(self.user))
        return await connect(ws.websocket_app, f"/ws/session/{sid or self.session.id}/",
                             cookie=cookie, origin=origin)

    def agent(self, token=HOST_TOKEN, linked=False, sid=None):
        headers = [(b"authorization", f"Bearer {token}".encode())]
        if linked:
            headers.append((b"x-vigil-link", link_token().encode()))
        return connect(ws.websocket_app, f"/ws/agent/session/{sid or self.session.id}/",
                       origin="", headers=headers)

    def refresh(self) -> RemoteSession:
        return RemoteSession.objects.get(pk=self.session.pk)

    def audits(self, action):
        from apps_business.audits.models import AuditEvent

        return AuditEvent.objects.filter(action=action)


class HappyPathTests(_SocketCase):
    async def test_a_session_pipes_both_ways_and_is_written_down(self):
        browser = await self.browser()
        self.assertEqual(await browser.receive_json(), {"t": "status", "state": "waiting"})

        agent = await self.agent()
        self.assertTrue(await agent.answered())
        self.assertEqual(await browser.receive_json(), {"t": "status", "state": "open"})
        session = await _get(self.refresh)
        self.assertEqual(session.state, "open")

        await browser.send_json({"t": "in", "d": _b64("ls\n")})
        self.assertEqual(await agent.receive_json(), {"t": "in", "d": _b64("ls\n")})
        await agent.send_json({"t": "out", "d": _b64("file.txt\n")})
        self.assertEqual(await browser.receive_json(), {"t": "out", "d": _b64("file.txt\n")})

        await browser.close()
        self.assertEqual((await agent.receive_json())["t"], "close")
        await agent.close()

        session = await _get(self.refresh)
        self.assertEqual(session.state, "closed")
        self.assertEqual((session.bytes_in, session.bytes_out), (3, 9))
        self.assertTrue(await _get(lambda: self.audits("remote.session_ended").exists()))

    async def test_an_open_session_outlives_the_agent_answer_limit(self):
        # The pending limit is for a session no agent ever joined; one that
        # opened must not be torn down by it.
        with patch.object(ws, "AGENT_ANSWER_SECONDS", 0.2), patch.object(ws, "WATCHDOG_TICK", 0.05):
            browser = await self.browser()
            await browser.receive_json()
            agent = await self.agent()
            await browser.receive_json()
            await _sleep(0.5)
            await agent.send_json({"t": "out", "d": _b64("still here")})
            self.assertEqual((await browser.receive_json())["t"], "out")
            await browser.close()
            await agent.close()
        self.assertEqual((await _get(self.refresh)).close_reason, "browser closed")

    async def test_an_agent_that_disconnects_ends_the_browser_side_too(self):
        browser = await self.browser()
        await browser.receive_json()
        agent = await self.agent()
        await browser.receive_json()
        await agent.close()
        last = await browser.drain_to_close()
        self.assertEqual(last, {"t": "status", "state": "closed",
                                "reason": "the agent disconnected"})


class BrowserRefusalTests(_SocketCase):
    async def test_a_foreign_origin_is_refused(self):
        conn = await self.browser(origin="http://evil.example")
        await conn.settle()
        self.assertEqual(conn.closed_code, 4403)
        self.assertFalse(conn.accepted)

    async def test_an_allowed_host_that_is_not_this_page_is_refused(self):
        # ALLOWED_HOSTS lists hosts Vigil answers to, not sites allowed to
        # script it: a wildcard there must not open the door.
        with override_settings(ALLOWED_HOSTS=["*"]):
            conn = await self.browser(origin="http://evil.example")
            await conn.settle()
        self.assertEqual(conn.closed_code, 4403)

    async def test_no_origin_is_refused(self):
        conn = await self.browser(origin="")
        await conn.settle()
        self.assertEqual(conn.closed_code, 4403)

    async def test_no_login_is_refused(self):
        conn = await self.browser(cookie="")
        await conn.settle()
        self.assertEqual(conn.closed_code, 4401)

    async def test_someone_elses_session_is_not_found(self):
        mallory = await _get(lambda: self._user("mallory"))
        cookie = await _get(lambda: self.cookie_for(mallory))
        conn = await self.browser(cookie=cookie)
        await conn.settle()
        self.assertEqual(conn.closed_code, 4404)

    async def test_a_second_browser_is_refused(self):
        first = await self.browser()
        await first.receive_json()
        second = await self.browser()
        await second.settle()
        self.assertEqual(second.closed_code, 4409)
        await first.close()


class AgentRefusalTests(_SocketCase):
    async def test_another_hosts_token_is_refused(self):
        conn = await self.agent(token=OTHER_TOKEN)
        await conn.settle()
        self.assertEqual(conn.closed_code, 4401)
        self.assertFalse(conn.accepted)

    async def test_a_closed_session_is_not_found(self):
        await _get(lambda: RemoteSession.objects.filter(pk=self.session.pk)
                   .update(state=RemoteSession.State.CLOSED))
        conn = await self.agent()
        await conn.settle()
        self.assertEqual(conn.closed_code, 4404)

    async def test_a_second_agent_is_refused(self):
        first = await self.agent()
        self.assertTrue(await first.answered())
        second = await self.agent()
        await second.settle()
        self.assertEqual(second.closed_code, 4409)
        await first.close()

    async def test_direct_is_refused_while_the_builtin_relay_is_off(self):
        await _get(_builtin_relay_off)
        conn = await self.agent()
        await conn.settle()
        self.assertEqual(conn.closed_code, 4403)

    async def test_through_the_link_is_accepted_while_the_builtin_relay_is_off(self):
        await _get(_builtin_relay_off)
        conn = await self.agent(linked=True)
        self.assertTrue(await conn.answered())
        await conn.close()


class LimitTests(_SocketCase):
    async def test_an_idle_session_is_closed(self):
        with patch.object(ws, "IDLE_SECONDS", 0.3), patch.object(ws, "WATCHDOG_TICK", 0.05):
            browser = await self.browser()
            await browser.receive_json()
            agent = await self.agent()
            last = await browser.drain_to_close(timeout=5)
            await agent.close()
        self.assertEqual(last["reason"], "idle timeout")
        self.assertEqual((await _get(self.refresh)).close_reason, "idle timeout")

    async def test_a_session_no_agent_answers_expires(self):
        with patch.object(ws, "AGENT_ANSWER_SECONDS", 0.3), patch.object(ws, "WATCHDOG_TICK", 0.05):
            browser = await self.browser()
            last = await browser.drain_to_close(timeout=5)
        self.assertEqual(last["reason"], "the agent did not answer")
        self.assertEqual((await _get(self.refresh)).state, "expired")


class RecordingTests(_SocketCase):
    async def _session_with_output(self):
        browser = await self.browser()
        await browser.receive_json()
        agent = await self.agent()
        await browser.receive_json()
        await agent.send_json({"t": "out", "d": _b64("secret-output")})
        await browser.receive_json()
        await browser.close()
        await agent.close()
        return await _get(self.refresh)

    async def test_business_records_the_output(self):
        with patch("apps.remote.recording.has_feature", return_value=True), \
                self.settings(MEDIA_ROOT=Path(self._tmp())):
            session = await self._session_with_output()
            path = Path(settings.MEDIA_ROOT) / session.recording
            lines = path.read_text().splitlines()
            mode = path.stat().st_mode & 0o777
        self.assertEqual(json.loads(lines[0])["version"], 2)
        self.assertEqual(json.loads(lines[1])[1:], ["o", "secret-output"])
        self.assertEqual(mode, 0o600)
        # Not guessable from the session id, which the list API hands out.
        self.assertNotEqual(Path(session.recording).stem, str(session.id))

    async def test_community_records_nothing(self):
        with patch("apps.remote.recording.has_feature", return_value=False), \
                self.settings(MEDIA_ROOT=Path(self._tmp())):
            session = await self._session_with_output()
            self.assertEqual(session.recording, "")
            self.assertFalse((Path(settings.MEDIA_ROOT) / "recordings").exists())

    def _tmp(self) -> str:
        import shutil
        import tempfile

        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


async def _get(fn):
    from asgiref.sync import sync_to_async

    return await sync_to_async(fn, thread_sensitive=True)()


async def _sleep(seconds):
    import asyncio

    await asyncio.sleep(seconds)


def _builtin_relay_off():
    # TransactionTestCase flushes the row the data migration seeded.
    Relay.objects.update_or_create(kind=Relay.Kind.BUILTIN,
                                   defaults={"name": "Built-in", "enabled": False})

