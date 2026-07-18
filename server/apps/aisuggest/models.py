"""AI-assisted debugging — free, bring-your-own endpoint (SQSY-LICENSING.md §1).

The operator supplies their own OpenAI-compatible or Anthropic endpoint;
Vigil never pays per-token inference and Business gets no hosted model either
(§2: same code path is load-bearing for both tiers, so Free costs nothing).
LLM output is untrusted: every suggestion passes through the task editor's
``parse_and_validate`` before a human ever sees it, and nothing is ever
auto-executed — suggestions land as *draft YAML*, full stop.
"""

from django.db import models

from apps.hosts.crypto import decrypt_secret, encrypt_secret


class AiSettings(models.Model):
    """Singleton row: where the operator's model lives."""

    class Provider(models.TextChoices):
        OPENAI_COMPAT = "openai", "OpenAI-compatible (incl. Ollama, vLLM, LM Studio)"
        ANTHROPIC = "anthropic", "Anthropic"

    provider = models.CharField(
        max_length=20, choices=Provider.choices, default=Provider.OPENAI_COMPAT)
    base_url = models.URLField(blank=True, default="")
    model = models.CharField(max_length=200, blank=True, default="")
    api_key_encrypted = models.BinaryField(blank=True, default=b"")
    enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def api_key(self) -> str:
        return decrypt_secret(self.api_key_encrypted)

    @api_key.setter
    def api_key(self, value: str) -> None:
        self.api_key_encrypted = encrypt_secret(value or "")

    @classmethod
    def get(cls) -> "AiSettings":
        row = cls.objects.first()
        return row if row is not None else cls.objects.create()

    def __str__(self) -> str:
        return f"ai:{self.provider}:{self.model or '(unset)'}"
