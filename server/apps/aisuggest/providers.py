"""Pluggable LLM providers. Two implementations, one contract:

``complete(system, user) -> str`` — one prompt in, one text answer out.
Deliberately no streaming, no tools, no chat history: a suggestion is one
round trip against the operator's own endpoint.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request


def _timeout() -> int:
    """Call timeout. Local BYO endpoints (Ollama on a homelab box) can take
    minutes on a cold model load — 60s punished exactly the users this
    feature is free for. Overridable per install."""
    from apps.instance.config import setting

    return int(setting("VIGIL_AI_TIMEOUT_SECONDS"))


class ProviderError(Exception):
    pass


#: A reasoning model served without a reasoning parser puts its thinking
#: inline. It is not part of the answer, and a ``` fence inside it would be
#: read as a suggestion.
_THINK = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)


def strip_thinking(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    timeout = _timeout()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # The endpoint's own error body says what is wrong (an unknown model,
        # a bad key, a parameter it does not take); the status line alone
        # does not.
        try:
            detail = exc.read().decode(errors="replace")[:400]
        except Exception:  # noqa: BLE001
            detail = ""
        raise ProviderError(f"endpoint returned HTTP {exc.code}: {detail or exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001 — normalized for the caller
        # A timeout arrives bare from a read, or wrapped in a URLError from
        # the connect; either way the operator needs to know what to change.
        if isinstance(exc, TimeoutError) or "timed out" in str(exc):
            raise ProviderError(
                f"no answer within {timeout}s — a reasoning model may need a lower "
                "reasoning effort, or raise the AI request timeout in Settings"
            ) from exc
        raise ProviderError(f"endpoint call failed: {exc}") from exc


class OpenAICompatProvider:
    """Any /v1/chat/completions endpoint: OpenAI, Ollama, vLLM, LM Studio…"""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 reasoning_effort: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort

    def complete(self, system: str, user: str) -> str:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        # Only sent when set: a model that does not reason rejects the field
        # on some servers. A thinking model left at its default can spend
        # minutes reasoning before it writes a line of YAML.
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        data = _post_json(f"{self.base_url}/chat/completions", payload, headers)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}") from exc
        text = strip_thinking(content or "")
        if not text:
            raise ProviderError(
                "the model returned an empty answer — a reasoning model may have "
                "spent its whole output on thinking; try a lower reasoning effort"
            )
        return text


class AnthropicProvider:
    """Direct Anthropic Messages API (no SDK — one POST)."""

    def __init__(self, base_url: str, model: str, api_key: str):
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.model = model
        self.api_key = api_key

    def complete(self, system: str, user: str) -> str:
        data = _post_json(
            f"{self.base_url}/v1/messages",
            {
                "model": self.model,
                "max_tokens": 2048,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
        )
        try:
            text = "".join(
                block["text"] for block in data["content"] if block["type"] == "text"
            )
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}") from exc
        if not text.strip():
            raise ProviderError("the model returned an empty answer")
        return text


def provider_for(row):
    """Build a callable client from an AiProvider row."""
    from .models import ProviderKind

    if row.kind == ProviderKind.ANTHROPIC:
        return AnthropicProvider(row.base_url, row.model, row.api_key)
    return OpenAICompatProvider(row.base_url, row.model, row.api_key,
                                row.reasoning_effort)
