"""What makes a model's answer usable: the prompt it is given, the second
chance a rejected task gets, and what the operator is told when nothing
comes back. Providers are faked; the endpoint is never called."""

import io
import json
import urllib.error
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.alerts.models import Alert, AlertRule
from apps.hosts.models import Host
from apps.tasks.registry import ACTION_REGISTRY
from apps.tasks.spec import SpecError, parse_and_validate

from .models import AiProvider
from .prompt import HIDDEN_ACTIONS, action_catalog, repair_prompt, system_prompt
from .providers import OpenAICompatProvider, ProviderError, strip_thinking

GOOD = """name: Start nginx
risk: standard
actions:
  - id: start
    type: start_service
    params:
      service_name: nginx
"""

BAD_INPUT = """name: Restart a service
inputs:
  - id: svc
    type: string
actions:
  - id: restart
    type: restart_service
    params:
      service_name: ${{ inputs.svc }}
"""

FIXED_INPUT = BAD_INPUT.replace("type: string", "type: text")


def fence(*blocks):
    return "\n".join(f"```yaml\n{b}```" for b in blocks)


def fake_provider(*answers):
    client = mock.Mock(complete=mock.Mock(side_effect=list(answers)))
    return mock.patch("apps.aisuggest.views.provider_for", return_value=client), client


class PromptTests(SimpleTestCase):
    def test_the_catalog_lists_every_offered_action_with_its_params(self):
        catalog = action_catalog()
        for name, spec in ACTION_REGISTRY.items():
            line = next((ln for ln in catalog.splitlines() if ln.startswith(f"- {name}(")), None)
            if name in HIDDEN_ACTIONS:
                self.assertIsNone(line, name)
                continue
            self.assertIsNotNone(line, name)
            for param in spec["required"]:
                self.assertIn(param, line)
            for param in spec.get("optional") or []:
                self.assertIn(f"{param}?", line)

    def test_dangerous_actions_are_never_offered(self):
        for name in ("update_agent", "stack_deploy", "stack_read"):
            self.assertNotIn(f"- {name}(", action_catalog())

    def test_the_prompt_teaches_what_models_got_wrong(self):
        prompt = system_prompt()
        self.assertIn("never string", prompt)
        self.assertIn('"ok", "error" or "skipped"', prompt)
        self.assertIn("execute_script with shell", prompt)
        self.assertIn("needs no relevant: block", prompt)

    def test_the_example_in_the_prompt_is_a_valid_task(self):
        example = system_prompt().split("```yaml\n", 1)[1].split("```", 1)[0]
        parse_and_validate(example)

    def test_the_repair_prompt_carries_each_reason(self):
        text = repair_prompt("Alert: x", [{"yaml": "name: a", "error": "bad type"}])
        self.assertIn("Alert: x", text)
        self.assertIn("Rejected because: bad type", text)
        self.assertIn("name: a", text)


class StatusValueTests(SimpleTestCase):
    def task(self, cond):
        return f"""name: t
actions:
  - id: start
    type: start_service
    params: {{service_name: nginx}}
  - id: restart
    type: restart_service
    params: {{service_name: nginx}}
    when: {cond}
"""

    def test_a_real_status_is_accepted(self):
        for status in ("ok", "error", "skipped"):
            parse_and_validate(self.task(f'steps.start.status == "{status}"'))
        parse_and_validate(self.task('steps.start.status != "ok"'))

    def test_an_invented_status_is_rejected(self):
        for status in ("success", "failed"):
            with self.assertRaisesRegex(SpecError, "status is one of ok, error, skipped"):
                parse_and_validate(self.task(f'steps.start.status == "{status}"'))

    def test_a_branch_condition_is_checked_too(self):
        yaml = """name: t
actions:
  - id: start
    type: start_service
    params: {service_name: nginx}
  - if: steps.start.status == "success"
    then:
      - id: check
        type: check_service
        params: {service_name: nginx}
"""
        with self.assertRaisesRegex(SpecError, "not 'success'"):
            parse_and_validate(yaml)


class ProviderTests(SimpleTestCase):
    def _call(self, provider, reply):
        body = json.dumps(reply).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body
        with mock.patch("apps.aisuggest.providers._timeout", return_value=5), \
                mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            text = provider.complete("sys", "user")
        sent = json.loads(urlopen.call_args.args[0].data)
        return text, sent

    def reply(self, content):
        return {"choices": [{"message": {"content": content}}]}

    def test_reasoning_effort_is_sent_only_when_set(self):
        _, sent = self._call(OpenAICompatProvider("http://x/v1", "m"), self.reply("hi"))
        self.assertNotIn("reasoning_effort", sent)
        _, sent = self._call(OpenAICompatProvider("http://x/v1", "m", "", "low"), self.reply("hi"))
        self.assertEqual(sent["reasoning_effort"], "low")

    def test_inline_thinking_is_dropped(self):
        text, _ = self._call(OpenAICompatProvider("http://x/v1", "m"),
                             self.reply("<think>```yaml\nname: no\n```</think>\nanswer"))
        self.assertEqual(text, "answer")
        self.assertEqual(strip_thinking("<think>never closed"), "")

    def test_an_empty_answer_says_why(self):
        with self.assertRaisesRegex(ProviderError, "lower reasoning effort"):
            self._call(OpenAICompatProvider("http://x/v1", "m"), self.reply(""))

    def test_an_http_error_carries_the_endpoint_body(self):
        err = urllib.error.HTTPError("http://x", 404, "Not Found", {},
                                     io.BytesIO(b'{"error": "model qwen not found"}'))
        with mock.patch("apps.aisuggest.providers._timeout", return_value=5), \
                mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(ProviderError, "model qwen not found"):
                OpenAICompatProvider("http://x/v1", "m").complete("s", "u")

    def test_a_timeout_names_the_fix(self):
        with mock.patch("apps.aisuggest.providers._timeout", return_value=5), \
                mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(ProviderError, "within 5s.*reasoning effort"):
                OpenAICompatProvider("http://x/v1", "m").complete("s", "u")


class SuggestFlowTests(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_user("root", password="x", is_staff=True)
        self.client.force_login(admin)
        self.provider = AiProvider.objects.create(
            name="local", base_url="http://byo.example/v1", model="qwen")
        self.host = Host.objects.create(hostname="web-02", os="Ubuntu 24.04",
                                        agent_token=uuid.uuid4().hex)
        rule = AlertRule.objects.create(name="Memory high", category="memory",
                                        metric="mem_pct", operator="gt", threshold=90,
                                        duration_seconds=300, severity="warning")
        self.alert = Alert.objects.create(host=self.host, rule=rule, severity="warning",
                                          message="memory 96% used", metric_value=96.4)

    def post(self):
        return self.client.post(f"/api/v1/ai/suggest/alert/{self.alert.id}/",
                                {"provider_id": self.provider.id})

    def test_a_rejected_task_gets_one_repair_and_the_fix_is_kept(self):
        patch, client = fake_provider(fence(GOOD, BAD_INPUT), fence(FIXED_INPUT))
        with patch:
            d = self.post().json()
        self.assertEqual(client.complete.call_count, 2)
        self.assertIn("unknown type 'string'", client.complete.call_args_list[1].args[1])
        self.assertEqual([s["parsed"]["name"] for s in d["suggestions"]],
                         ["Start nginx", "Restart a service"])
        self.assertEqual(d["rejected"], [])

    def test_what_the_repair_cannot_fix_is_shown_with_its_reason(self):
        patch, _ = fake_provider(fence(BAD_INPUT), fence(BAD_INPUT))
        with patch:
            d = self.post().json()
        self.assertEqual(d["suggestions"], [])
        self.assertEqual(len(d["rejected"]), 1)
        self.assertIn("unknown type 'string'", d["rejected"][0]["error"])
        self.assertIn("type: string", d["rejected"][0]["yaml"])

    def test_no_repair_when_everything_passed(self):
        patch, client = fake_provider(fence(GOOD))
        with patch:
            d = self.post().json()
        self.assertEqual(client.complete.call_count, 1)
        self.assertEqual(len(d["suggestions"]), 1)

    def test_a_failed_repair_keeps_the_first_answer(self):
        patch, _ = fake_provider(fence(GOOD, BAD_INPUT), ProviderError("timed out"))
        with patch:
            resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["suggestions"]), 1)
        self.assertEqual(len(resp.json()["rejected"]), 1)

    def test_prose_without_a_task_comes_back_as_a_note(self):
        patch, _ = fake_provider("The advisory gives no way to mitigate this.")
        with patch:
            d = self.post().json()
        self.assertEqual(d["suggestions"], [])
        self.assertEqual(d["note"], "The advisory gives no way to mitigate this.")

    def test_the_alert_prompt_carries_the_rule_and_the_host(self):
        self.host.container_engines = [{"kind": "docker", "version": "27.3.1"}]
        self.host.save()
        patch, client = fake_provider(fence(GOOD))
        with patch:
            self.post()
        prompt = client.complete.call_args.args[1]
        self.assertIn("Rule: Memory high (memory): mem_pct > 90 for 300s", prompt)
        self.assertIn("Current value: 96.4", prompt)
        self.assertIn("OS: Ubuntu 24.04", prompt)
        self.assertIn("Container engine: docker 27.3.1", prompt)

    def test_a_host_without_an_engine_says_so(self):
        patch, client = fake_provider(fence(GOOD))
        with patch:
            self.post()
        self.assertIn("none reported, so do not use container actions",
                      client.complete.call_args.args[1])


class ProviderSettingsTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("root", password="x", is_staff=True)
        self.client.force_login(self.admin)

    def test_reasoning_effort_is_saved_and_validated(self):
        r = self.client.post("/api/v1/ai/providers/", {
            "name": "q", "base_url": "http://x/v1", "model": "m", "reasoning_effort": "low"},
            content_type="application/json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["reasoning_effort"], "low")
        pid = r.json()["id"]
        r = self.client.patch(f"/api/v1/ai/providers/{pid}/", {"reasoning_effort": "extreme"},
                              content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"/api/v1/ai/providers/{pid}/", {"reasoning_effort": ""},
                              content_type="application/json")
        self.assertEqual(r.json()["reasoning_effort"], "")

    def test_the_connection_test_reports_an_accepted_task(self):
        p = AiProvider.objects.create(name="q", base_url="http://x/v1", model="m")
        patch, _ = fake_provider(fence(GOOD))
        with patch:
            d = self.client.post(f"/api/v1/ai/providers/{p.id}/test/").json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["accepted"], 1)
        self.assertIn("elapsed_ms", d)

    def test_the_connection_test_reports_why_it_failed(self):
        p = AiProvider.objects.create(name="q", base_url="http://x/v1", model="m")
        patch, _ = fake_provider(ProviderError("endpoint returned HTTP 401: bad key"))
        with patch:
            d = self.client.post(f"/api/v1/ai/providers/{p.id}/test/").json()
        self.assertFalse(d["ok"])
        self.assertIn("401", d["error"])

    def test_only_an_admin_can_test(self):
        p = AiProvider.objects.create(name="q", base_url="http://x/v1", model="m")
        get_user_model().objects.create_user("v", password="x")
        c = self.client_class()
        c.login(username="v", password="x")
        self.assertEqual(c.post(f"/api/v1/ai/providers/{p.id}/test/").status_code, 403)
