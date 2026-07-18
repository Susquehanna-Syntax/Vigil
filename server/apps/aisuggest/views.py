"""Suggest-a-fix API. LLM output is untrusted input: suggestions are parsed
with the same validator as the task editor, invalid ones are dropped, and
nothing is ever executed — the human gets draft YAML to review."""

import logging
import re

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin, IsOperator
from apps.alerts.models import Alert
from apps.tasks.spec import SpecError, parse_and_validate

from .models import AiSettings
from .providers import ProviderError, provider_for

logger = logging.getLogger("vigil.aisuggest")

SYSTEM_PROMPT = """You are Vigil's remediation assistant. Given an \
infrastructure alert, propose 1-3 remediation tasks as Vigil task YAML.

Rules:
- Output ONLY fenced yaml blocks (```yaml ... ```), one per suggestion.
- Each block: name, description, risk (low|standard|high), actions (list of
  {type, params}).
- Prefer low-risk, reversible diagnostics before invasive fixes.
- Never propose update_agent."""


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def ai_settings(request):
    row = AiSettings.get()
    if request.method == "POST":
        row.provider = request.data.get("provider", row.provider)
        row.base_url = (request.data.get("base_url") or "").strip()
        row.model = (request.data.get("model") or "").strip()
        if "api_key" in request.data:
            row.api_key = request.data.get("api_key") or ""
        row.enabled = bool(request.data.get("enabled", row.enabled))
        row.save()
    return Response({
        "provider": row.provider,
        "base_url": row.base_url,
        "model": row.model,
        "api_key_set": bool(row.api_key_encrypted),
        "enabled": row.enabled,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOperator])
def suggest_for_alert(request, alert_id):
    row = AiSettings.get()
    if not (row.enabled and row.base_url or
            row.enabled and row.provider == AiSettings.Provider.ANTHROPIC):
        return Response(
            {"detail": "AI suggestions are not configured. Point Vigil at any "
                       "OpenAI-compatible or Anthropic endpoint in Settings — "
                       "bring your own key; nothing is hosted by SQSY."},
            status=409,
        )
    alert = get_object_or_404(Alert, pk=alert_id)
    prompt = _alert_prompt(alert)
    try:
        text = provider_for(row).complete(SYSTEM_PROMPT, prompt)
    except ProviderError as exc:
        logger.warning("suggestion call failed: %s", exc)
        return Response({"detail": str(exc)}, status=502)

    suggestions = []
    for block in re.findall(r"```(?:yaml)?\s*\n(.*?)```", text, flags=re.S):
        try:
            spec = parse_and_validate(block)
        except SpecError as exc:
            logger.info("dropping invalid suggestion: %s", exc)
            continue
        if any(a.get("type") == "update_agent" for a in spec.get("actions", [])):
            continue  # the one action a model must never hand out
        suggestions.append({"yaml": block.strip(), "parsed": spec})
        if len(suggestions) == 3:
            break
    return Response({"suggestions": suggestions,
                     "raw_count": text.count("```") // 2})


def _alert_prompt(alert) -> str:
    host = getattr(alert, "host", None)
    lines = [f"Alert: {alert}"]
    if host is not None:
        lines += [f"Host: {host.hostname}", f"OS: {host.os}",
                  f"Tags: {', '.join(map(str, host.tags or []))}"]
    for attr in ("message", "severity", "metric_value"):
        v = getattr(alert, attr, None)
        if v:
            lines.append(f"{attr.capitalize()}: {v}")
    return "\n".join(lines)
