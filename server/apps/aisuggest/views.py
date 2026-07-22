"""AI suggest API. Providers are configured per operator (BYO endpoint/key).
A suggest call runs ONE provider and returns its validated suggestions plus
timing, so the frontend can fan out to several providers in parallel, show a
loading state per provider, and compare the results. LLM output is untrusted:
everything passes through parse_and_validate, update_agent is dropped on sight,
and nothing is auto-executed — the human picks."""

import logging
import re
import time

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin, IsOperator
from apps.alerts.models import Alert
from apps.tasks.spec import SpecError, parse_and_validate

from .models import AiProvider, ProviderKind
from .providers import ProviderError, provider_for

logger = logging.getLogger("vigil.aisuggest")

SYSTEM_PROMPT = """You are Vigil's remediation assistant. Given an \
infrastructure problem, propose 1-3 remediation tasks as Vigil task YAML.

Rules:
- Output ONLY fenced yaml blocks (```yaml ... ```), one per suggestion.
- Each block: name, description, risk (low|standard|high), actions (list of
  {type, params}).
- Prefer low-risk, reversible diagnostics before invasive fixes.
- Never propose update_agent."""


def _provider_dict(p: "AiProvider", *, with_key_state=False) -> dict:
    d = {
        "id": p.id,
        "name": p.name,
        "kind": p.kind,
        "base_url": p.base_url,
        "model": p.model,
        "enabled": p.enabled,
        "configured": p.configured,
        "order": p.order,
    }
    if with_key_state:
        d["api_key_set"] = bool(p.api_key_encrypted)
    return d


# ── Provider management ────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def providers(request):
    if request.method == "GET":
        return Response([_provider_dict(p, with_key_state=True)
                         for p in AiProvider.objects.all()])
    p = AiProvider(
        name=(request.data.get("name") or "New provider").strip(),
        kind=request.data.get("kind") or ProviderKind.OPENAI_COMPAT,
        base_url=(request.data.get("base_url") or "").strip(),
        model=(request.data.get("model") or "").strip(),
        enabled=bool(request.data.get("enabled", True)),
        order=AiProvider.objects.count(),
    )
    if request.data.get("api_key"):
        p.api_key = request.data["api_key"]
    p.save()
    return Response(_provider_dict(p, with_key_state=True), status=201)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsAdmin])
def provider_detail(request, provider_id):
    p = get_object_or_404(AiProvider, pk=provider_id)
    if request.method == "DELETE":
        p.delete()
        return Response(status=204)
    for field in ("name", "base_url", "model"):
        if field in request.data:
            setattr(p, field, (request.data[field] or "").strip())
    if "kind" in request.data:
        p.kind = request.data["kind"]
    if "enabled" in request.data:
        p.enabled = bool(request.data["enabled"])
    if "order" in request.data:
        p.order = int(request.data["order"])
    if request.data.get("api_key"):
        p.api_key = request.data["api_key"]
    p.save()
    return Response(_provider_dict(p, with_key_state=True))


# ── Suggestion runs ────────────────────────────────────────────────────────

def _run_provider(provider_id: int, prompt: str) -> Response:
    provider = AiProvider.objects.filter(pk=provider_id, enabled=True).first()
    if provider is None:
        return Response({"detail": "provider not found or disabled"}, status=404)
    if not provider.configured:
        return Response({"detail": f"{provider.name} is missing a model/URL"},
                        status=409)
    started = time.monotonic()
    try:
        text = provider_for(provider).complete(SYSTEM_PROMPT, prompt)
    except ProviderError as exc:
        logger.warning("suggestion via %s failed: %s", provider.name, exc)
        return Response({"provider": _provider_dict(provider), "error": str(exc),
                         "elapsed_ms": int((time.monotonic() - started) * 1000)},
                        status=502)
    return Response({
        "provider": _provider_dict(provider),
        "suggestions": _extract_suggestions(text),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOperator])
def suggest_for_alert(request, alert_id):
    if not AiProvider.objects.filter(enabled=True).exists():
        return _no_providers()
    alert = get_object_or_404(Alert, pk=alert_id)
    provider_id = request.data.get("provider_id")
    if not provider_id:
        return Response({"detail": "provider_id required"}, status=400)
    return _run_provider(int(provider_id), _alert_prompt(alert))


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOperator])
def suggest_for_container(request, host_id, container_id):
    from apps.hosts.models import DockerContainer, Host

    if not AiProvider.objects.filter(enabled=True).exists():
        return _no_providers()
    host = get_object_or_404(Host, pk=host_id)
    container = get_object_or_404(DockerContainer, host=host,
                                  container_id=container_id)
    provider_id = request.data.get("provider_id")
    if not provider_id:
        return Response({"detail": "provider_id required"}, status=400)
    return _run_provider(int(provider_id), _container_prompt(host, container,
                         (request.data.get("note") or "").strip()[:500]))


def _no_providers():
    return Response(
        {"detail": "No AI providers are configured. Add one in Settings — bring "
                   "your own OpenAI-compatible or Anthropic endpoint; nothing "
                   "is hosted by SQSY."},
        status=409,
    )


def _extract_suggestions(text: str) -> list[dict]:
    out = []
    for block in re.findall(r"```(?:yaml)?\s*\n(.*?)```", text, flags=re.S):
        try:
            spec = parse_and_validate(block)
        except SpecError as exc:
            logger.info("dropping invalid suggestion: %s", exc)
            continue
        if any(a.get("type") == "update_agent" for a in spec.get("actions", [])):
            continue
        out.append({"yaml": block.strip(), "parsed": spec,
                    "risk": spec.get("derived_risk") or spec.get("risk", "standard")})
        if len(out) == 3:
            break
    return out


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


def _container_prompt(host, container, note: str) -> str:
    lines = [
        f"Docker container issue on host {host.hostname} ({host.os})",
        f"Container: {container.name} (image {container.image})",
        f"State: {container.state} — {container.status}",
    ]
    if container.stack:
        lines.append(f"Compose stack/service: {container.stack}/{container.service}")
    if container.cpu_percent is not None:
        lines.append(f"CPU: {container.cpu_percent:.1f}%")
    if container.mem_percent is not None:
        lines.append(f"Memory: {container.mem_percent:.1f}%")
    if note:
        lines.append(f"Operator note: {note}")
    return "\n".join(lines)
