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

from .models import AiProvider, ProviderKind, ReasoningEffort
from .prompt import repair_prompt, system_prompt
from .providers import ProviderError, provider_for

logger = logging.getLogger("vigil.aisuggest")

SYSTEM_PROMPT = system_prompt()


def _provider_dict(p: "AiProvider", *, with_key_state=False) -> dict:
    d = {
        "id": p.id,
        "name": p.name,
        "kind": p.kind,
        "base_url": p.base_url,
        "model": p.model,
        "reasoning_effort": p.reasoning_effort,
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
        return Response(
            [_provider_dict(p, with_key_state=True) for p in AiProvider.objects.all()]
        )
    p = AiProvider(
        name=(request.data.get("name") or "New provider").strip(),
        kind=request.data.get("kind") or ProviderKind.OPENAI_COMPAT,
        base_url=(request.data.get("base_url") or "").strip(),
        model=(request.data.get("model") or "").strip(),
        enabled=bool(request.data.get("enabled", True)),
        order=AiProvider.objects.count(),
    )
    effort = _effort(request.data.get("reasoning_effort"))
    if effort is None:
        return _bad_effort()
    p.reasoning_effort = effort
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
    if "reasoning_effort" in request.data:
        effort = _effort(request.data["reasoning_effort"])
        if effort is None:
            return _bad_effort()
        p.reasoning_effort = effort
    if "enabled" in request.data:
        p.enabled = bool(request.data["enabled"])
    if "order" in request.data:
        p.order = int(request.data["order"])
    if request.data.get("api_key"):
        p.api_key = request.data["api_key"]
    p.save()
    return Response(_provider_dict(p, with_key_state=True))


def _effort(value):
    """A valid reasoning effort, "" for none, or None when it is not one."""
    value = (value or "").strip().lower()
    return value if value in ReasoningEffort.values else None


def _bad_effort():
    return Response(
        {"detail": "reasoning_effort must be blank, low, medium or high"}, status=400
    )


#: A request small enough to answer fast and specific enough to judge: a
#: provider that passes this can write a task Vigil accepts.
_TEST_PROMPT = (
    "Alert: [critical] web-02: nginx.service is inactive (failed)\n"
    "Host: web-02\nOS: Ubuntu 24.04\n"
    "Propose exactly one task."
)


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsAdmin])
def provider_test(request, provider_id):
    """Ask the provider for one real suggestion and report what came back:
    how long it took, whether Vigil accepted the task, and why not."""
    p = get_object_or_404(AiProvider, pk=provider_id)
    if not p.configured:
        return Response({"ok": False, "error": f"{p.name} is missing a model/URL"})
    started = time.monotonic()
    try:
        text = provider_for(p).complete(SYSTEM_PROMPT, _TEST_PROMPT)
    except ProviderError as exc:
        return Response({"ok": False, "error": str(exc),
                         "elapsed_ms": int((time.monotonic() - started) * 1000)})
    found = _extract_suggestions(text)
    return Response({
        "ok": bool(found["suggestions"]),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "accepted": len(found["suggestions"]),
        "rejected": found["rejected"],
        "note": found["note"],
        "reply": text[:2000],
    })


# ── Suggestion runs ────────────────────────────────────────────────────────


def _run_provider(provider_id: int, prompt: str) -> Response:
    provider = AiProvider.objects.filter(pk=provider_id, enabled=True).first()
    if provider is None:
        return Response({"detail": "provider not found or disabled"}, status=404)
    if not provider.configured:
        return Response(
            {"detail": f"{provider.name} is missing a model/URL"}, status=409
        )
    started = time.monotonic()
    client = provider_for(provider)
    try:
        text = client.complete(SYSTEM_PROMPT, prompt)
        found = _extract_suggestions(text)
        # One second chance for the tasks Vigil rejected, told exactly why.
        # Small models get a param name or an input type wrong far more often
        # than they get the remedy wrong, and the reason is enough to fix it.
        if found["rejected"] and len(found["suggestions"]) < MAX_SUGGESTIONS:
            try:
                retry = _extract_suggestions(
                    client.complete(SYSTEM_PROMPT, repair_prompt(prompt, found["rejected"]))
                )
            except ProviderError as exc:
                logger.info("repair via %s failed: %s", provider.name, exc)
            else:
                found = _merge(found, retry)
    except ProviderError as exc:
        logger.warning("suggestion via %s failed: %s", provider.name, exc)
        return Response(
            {
                "provider": _provider_dict(provider),
                "error": str(exc),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
            status=502,
        )
    return Response(
        {
            "provider": _provider_dict(provider),
            **found,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    )


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
def suggest_for_vuln(request, finding_id):
    """Suggest a remediation for one vulnerability finding.

    Scoped to the *package*, not the CVE. A single ``update_package openssl``
    clears every open openssl CVE on the host at once, so asking for a fix to
    one of them in isolation invites a suggestion that leaves the other twelve
    behind — and invites the operator to run it thirteen times.
    """
    from apps.vulns.models import VulnFinding

    if not AiProvider.objects.filter(enabled=True).exists():
        return _no_providers()
    finding = get_object_or_404(
        VulnFinding.objects.select_related("host"), pk=finding_id
    )
    provider_id = request.data.get("provider_id")
    if not provider_id:
        return Response({"detail": "provider_id required"}, status=400)
    return _run_provider(
        int(provider_id),
        _vuln_prompt(finding, (request.data.get("note") or "").strip()[:500]),
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated, IsOperator])
def suggest_for_fix_group(request):
    """Mitigations for a vulnerability with no fix (M9).

    Only offered when nothing can be upgraded: a group with a fixed version
    has a deterministic task, and an assistant guessing at an upgrade is the
    one thing this must never do. The prompt is grounded in what Vigil
    stored — the advisory's own text, the affected path, the vendor's status
    and where the thing is running — and says so.
    """
    from apps.vulns.fixview import _fix_for
    from apps.vulns.models import VulnFinding

    if not AiProvider.objects.filter(enabled=True).exists():
        return _no_providers()
    fix_key = str(request.data.get("fix_key") or "")
    findings = list(VulnFinding.objects.filter(fix_key=fix_key, state=VulnFinding.State.OPEN)
                    .select_related("host").prefetch_related("evidence")[:200])
    if not findings:
        return Response({"detail": "no open findings in that group"}, status=404)
    if _fix_for(findings[0])["kind"] != "none":
        return Response({"detail": "this group has a fix — deploy it rather than mitigate"},
                        status=400)
    provider_id = request.data.get("provider_id")
    if not provider_id:
        return Response({"detail": "provider_id required"}, status=400)
    return _run_provider(int(provider_id), mitigation_prompt(findings))


def mitigation_prompt(findings) -> str:
    """The no-fix prompt: everything Vigil knows, and the rule that binds it."""
    lead = max(findings, key=lambda f: (f.cvss_score or 0))
    cves = sorted({f.cve_id for f in findings if f.cve_id})
    lines = [
        "NO FIX IS AVAILABLE for this vulnerability. Propose mitigations only — disable the",
        "affected module or feature, block or bind a port, stop a service, remove an unused",
        "file — as Vigil task steps. Never propose upgrading, installing or pinning a version:",
        "none exists. Ground every step in the advisory text below; if it gives no way to",
        "mitigate, say that plainly instead of guessing.",
        "",
        f"Package: {lead.package_name or '(none)'} {lead.installed_version or ''}".rstrip(),
        f"Affected path: {lead.affected_path or '(not reported)'}",
        f"Vendor status: {lead.vendor_status or 'unknown'}",
        f"Severity: {lead.severity}" + (f", CVSS {lead.cvss_score}" if lead.cvss_score else ""),
        f"CVEs: {', '.join(cves) if cves else '(none)'}",
        f"Title: {lead.title}",
        "Advisory text:",
        (lead.description or "(the source gave no description)")[:4000],
    ]
    if lead.references:
        lines.append("References: " + ", ".join(lead.references[:5]))
    lines.append("Hosts and evidence:")
    for f in findings[:20]:
        evidence = "; ".join(e.summary for e in f.evidence.all()) or "scanner report only"
        lines.append(f"- {f.host.hostname} ({f.host.os or 'unknown OS'}): {evidence}")
    return "\n".join(lines)


def _no_providers():
    return Response(
        {
            "detail": "No AI providers are configured. Add one in Settings — bring "
            "your own OpenAI-compatible or Anthropic endpoint; nothing "
            "is hosted by SQSY."
        },
        status=409,
    )


#: Suggestions returned per provider.
MAX_SUGGESTIONS = 3

_FENCE = re.compile(r"```[ \t]*(?:ya?ml)?[ \t]*\n(.*?)```", re.S | re.I)


def _extract_suggestions(text: str) -> dict:
    """Split a model's answer into tasks Vigil accepts and ones it rejects.

    A rejected task comes back with the validator's reason, so the operator
    sees why a model produced nothing usable rather than an empty column. The
    prose outside the blocks is kept as ``note``: a model told to say plainly
    when no task can help does exactly that, and it is worth showing.
    """
    suggestions, rejected = [], []
    for block in _FENCE.findall(text):
        try:
            spec = parse_and_validate(block)
        except SpecError as exc:
            logger.info("rejecting suggestion: %s", exc)
            rejected.append({"yaml": block.strip(), "error": str(exc)})
            continue
        if any(a.get("type") == "update_agent" for a in spec.get("actions", [])):
            continue
        if len(suggestions) < MAX_SUGGESTIONS:
            suggestions.append(_suggestion(block, spec))
    note = _FENCE.sub("", text).strip()
    return {"suggestions": suggestions, "rejected": rejected[:MAX_SUGGESTIONS],
            "note": note[:1500]}


def _suggestion(block: str, spec: dict) -> dict:
    return {
        "yaml": block.strip(),
        "parsed": spec,
        "risk": spec.get("derived_risk") or spec.get("risk", "standard"),
    }


def _merge(first: dict, retry: dict) -> dict:
    """The first answer's tasks, then any the repair fixed. What the repair
    still could not get right replaces the original rejections."""
    seen = {s["yaml"] for s in first["suggestions"]}
    merged = list(first["suggestions"])
    for s in retry["suggestions"]:
        if s["yaml"] not in seen and len(merged) < MAX_SUGGESTIONS:
            merged.append(s)
            seen.add(s["yaml"])
    return {"suggestions": merged, "rejected": retry["rejected"],
            "note": first["note"] or retry["note"]}


def _host_lines(host) -> list[str]:
    """The facts about a host that decide which actions make sense on it."""
    lines = [
        f"Host: {host.hostname}",
        f"OS: {host.os or 'unknown'}" + (f" (kernel {host.kernel})" if host.kernel else ""),
    ]
    engines = [e for e in (host.container_engines or []) if isinstance(e, dict)]
    if engines:
        lines.append("Container engine: " + ", ".join(
            f"{e.get('kind', '?')} {e.get('version', '')}".strip() for e in engines))
    else:
        # Without this a model reaches for Docker cleanup on every disk alert.
        lines.append("Container engine: none reported, so do not use container actions")
    if host.tags:
        lines.append(f"Tags: {', '.join(map(str, host.tags))}")
    return lines


def _alert_prompt(alert) -> str:
    """What fired, against what threshold, on what kind of machine."""
    host = getattr(alert, "host", None)
    lines = [f"Alert: {alert}"]
    if host is not None:
        lines += _host_lines(host)
    rule = getattr(alert, "rule", None)
    if rule is not None:
        op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=="}.get(
            rule.operator, rule.operator)
        held = f" for {rule.duration_seconds}s" if rule.duration_seconds else ""
        lines.append(f"Rule: {rule.name} ({rule.category}): {rule.metric} {op} "
                     f"{rule.threshold:g}{held}")
    if alert.metric_value is not None:
        lines.append(f"Current value: {alert.metric_value:g}")
    lines.append(f"Severity: {alert.severity}")
    for key, value in (alert.fix_context or {}).items():
        if key in ("host_id",) or value in (None, ""):
            continue
        lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    return "\n".join(lines)


#: How many sibling CVEs to name before summarising the rest. Enough for the
#: model to see this is a package-level fix, short of burning the context
#: window on a package with two hundred open CVEs.
_SIBLING_LIMIT = 12


def _vuln_prompt(finding, note: str = "") -> str:
    """Describe a finding in the terms a remediation actually takes.

    The useful unit is the package and the version it needs to reach, not the
    CVE number: package managers upgrade packages. Trivy fills in
    ``package_name``/``installed_version``/``fixed_version``, so for its
    findings this is a precise instruction rather than a guess. Nessus network
    findings often carry none of that, and the prompt says so plainly instead
    of implying a package fix that cannot be written.
    """
    from apps.vulns.models import VulnFinding

    host = finding.host
    lines = [
        f"Vulnerability on host {host.hostname} ({host.os or 'unknown OS'})",
        f"Severity: {finding.get_severity_display()}",
    ]
    lines += [line for line in _host_lines(host) if line.startswith("Container engine")]
    if finding.cve_id:
        lines.append(f"CVE: {finding.cve_id}")
    if finding.title:
        lines.append(f"Title: {finding.title}")
    lines.append(f"Reported by: {finding.scanner}")

    if finding.package_name:
        lines.append(f"Package: {finding.package_name}")
        lines.append(f"Installed version: {finding.installed_version or 'unknown'}")
        lines.append(
            f"Fixed in version: {finding.fixed_version}"
            if finding.fixed_version
            else "Fixed version: not published by the scanner — no upgrade "
            "target is known, so a mitigation may be the only option."
        )

        # Everything else open against this package. One upgrade clears them
        # all, and the model should be told that rather than left to guess.
        siblings = list(
            VulnFinding.objects.filter(
                host=host,
                package_name=finding.package_name,
                state=VulnFinding.State.OPEN,
            )
            .exclude(pk=finding.pk)
            .order_by("cve_id")[: _SIBLING_LIMIT + 1]
        )
        if siblings:
            shown = [s.cve_id or s.plugin_id_or_oid for s in siblings[:_SIBLING_LIMIT]]
            extra = len(siblings) - len(shown)
            tail = f" (and {extra} more)" if extra > 0 else ""
            lines.append(
                f"This host has {len(siblings)} further open finding(s) "
                f"against the same package: {', '.join(shown)}{tail}. "
                f"One upgrade of {finding.package_name} should clear them "
                f"together — propose a single task, not one per CVE."
            )
    else:
        lines.append(
            "The scanner reported no package for this finding, so it is "
            "probably a service or configuration issue rather than something "
            "a package upgrade fixes. Prefer a diagnostic task that confirms "
            "the exposure before proposing any change."
        )

    if host.tags:
        lines.append(f"Host tags: {', '.join(map(str, host.tags))}")
    if note:
        lines.append(f"Operator note: {note}")
    return "\n".join(lines)
