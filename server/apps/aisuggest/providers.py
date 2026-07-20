"""Pluggable LLM providers. Two implementations, one contract:

``complete(system, user) -> str`` — one prompt in, one text answer out.
Deliberately no streaming, no tools, no chat history: a suggestion is one
round trip against the operator's own endpoint.
"""

from __future__ import annotations

import json
import urllib.request


def _timeout() -> int:
    """Call timeout. Local BYO endpoints (Ollama on a homelab box) can take
    minutes on a cold model load — 60s punished exactly the users this
    feature is free for. Overridable per install."""
    from django.conf import settings

    return int(getattr(settings, "VIGIL_AI_TIMEOUT_SECONDS", 300))


class ProviderError(Exception):
    pass


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — normalized for the caller
        raise ProviderError(f"endpoint call failed: {exc}") from exc


class OpenAICompatProvider:
    """Any /v1/chat/completions endpoint: OpenAI, Ollama, vLLM, LM Studio…"""

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def complete(self, system: str, user: str) -> str:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = _post_json(
            f"{self.base_url}/chat/completions",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
            },
            headers,
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}") from exc


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
            return "".join(
                block["text"] for block in data["content"] if block["type"] == "text"
            )
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}") from exc


def provider_for(settings_row):
    from .models import AiSettings

    if settings_row.provider == AiSettings.Provider.ANTHROPIC:
        return AnthropicProvider(settings_row.base_url, settings_row.model,
                                 settings_row.api_key)
    return OpenAICompatProvider(settings_row.base_url, settings_row.model,
                                settings_row.api_key)
