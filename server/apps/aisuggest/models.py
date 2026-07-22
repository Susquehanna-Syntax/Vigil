"""AI-assisted debugging — free, bring-your-own endpoint (SQSY-LICENSING.md §1).

The operator supplies their own OpenAI-compatible or Anthropic endpoints;
Vigil never pays per-token inference and Business gets no hosted model either
(§2: same code path is load-bearing for both tiers, so Free costs nothing).
LLM output is untrusted: every suggestion passes through the task editor's
``parse_and_validate`` before a human ever sees it, and nothing is ever
auto-executed — suggestions land as *draft YAML*, full stop.

Multiple providers can be configured so an operator can fan a request out to
several models at once and compare the results side by side before picking
one. Nothing about that changes the trust model: it's still the human
choosing which validated suggestion (if any) to run.
"""

from django.db import models

from apps.hosts.crypto import decrypt_secret, encrypt_secret


class ProviderKind(models.TextChoices):
    OPENAI_COMPAT = "openai", "OpenAI-compatible (Ollama, vLLM, LM Studio, OpenAI)"
    ANTHROPIC = "anthropic", "Anthropic"


class AiProvider(models.Model):
    """One configured model endpoint. Several may exist; enabled ones show up
    in the suggest picker and can be compared against each other."""

    name = models.CharField(max_length=100)  # operator-facing label
    kind = models.CharField(max_length=20, choices=ProviderKind.choices,
                            default=ProviderKind.OPENAI_COMPAT)
    base_url = models.URLField(blank=True, default="")
    model = models.CharField(max_length=200, blank=True, default="")
    api_key_encrypted = models.BinaryField(blank=True, default=b"")
    enabled = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("order", "created_at")

    @property
    def api_key(self) -> str:
        return decrypt_secret(self.api_key_encrypted)

    @api_key.setter
    def api_key(self, value: str) -> None:
        self.api_key_encrypted = encrypt_secret(value or "")

    @property
    def configured(self) -> bool:
        """Enough to actually call: a model, and a base URL unless Anthropic
        (which has a default endpoint)."""
        if not self.model:
            return False
        return bool(self.base_url) or self.kind == ProviderKind.ANTHROPIC

    def __str__(self) -> str:
        return f"{self.name} ({self.model or 'unset'})"
