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
- Each block: name, description, risk (low|standard|high), optional inputs, and actions as a
  list of `- id:` blocks, each with `type:` and a `params:` mapping on its own lines.
- Refer to an input as ${{ inputs.<id> }}. Never write {{ inputs.<id> }}.
- A later step may use an earlier step's declared outputs as
  ${{ steps.<id>.result.<field> }}, or its status as ${{ steps.<id>.status }};
  when: may test steps.<id>.result.<field>.
- A script receives inputs as environment variables VIGIL_INPUT_<ID>
  ($VIGIL_INPUT_APP in bash, $env:VIGIL_INPUT_APP in PowerShell); never paste
  an input into a command line.
- To answer "which hosts have X" (a file, package version, process, listening port, service,
  registry value, or text in files), use a hunt_* action rather than run_command or execute_script.
- Never set return: text on hunt_content unless the user asks for the matched text; it makes the
  task high risk.
- To make a task apply only to some hosts, add a top-level relevant: tree of all:/any:/not: lists
  whose items are hunt_* probes (e.g. - hunt_service: {name: nginx}); hosts where it is false report
  not applicable and run nothing. Do not use relevance: (free text) for this.
- To choose between steps, use an actions item with if: <expression>, then: [steps], else: [steps];
  conditions may read earlier steps' steps.<id>.status and steps.<id>.result.<field>.
- Write if: and when: conditions bare, never inside ${{ }}; booleans are True and False.
- <, <=, >, >= compare numbers only.
- Test only outputs a step really has: every hunt_* step outputs matched, count and truncated;
  check_service outputs active and state.
- A step may carry outcome: <short label> naming what finishing it means (e.g. Restarted); use that
  instead of a step that only prints a message.
- Quote any YAML value that contains ": ".
- For services use check_service / restart_service / start_service with params: {service_name: <name>},
  never run_command; an output is always read as steps.<id>.result.<field>.
- Prefer low-risk, reversible diagnostics before invasive fixes.
- To install, upgrade, pin or remove software use app_install / app_upgrade / app_pin / app_uninstall
  with params: {app: <package id>, source: <dpkg|rpm|apk|pacman|snap|flatpak|winget|chocolatey|registry>};
  omit source to use the host's own manager. app_upgrade without app upgrades everything outdated.
- Refresh the inventory with app_inventory (outputs count, outdated, unmanaged) and branch on it.
- Use app_install_custom (high risk, needs url + sha256) only when no package manager has the software.
- To keep one app in a state use app_ensure with app + state (present, latest, pinned, absent); version only with pinned.
  It reads the host's inventory and changes nothing when the host already matches, so it is safe to run on a schedule.
- App and patch policies are set on the Policies page, not in YAML. Never write or edit a task named "Policy: ..." —
  it is generated from its policy and rewritten on every save.
- A vulnerability is fixed per fix group: one upgrade of the package to its fixed version (app_upgrade) or one
  KB (windows_update_install include_kb) clears every CVE in the group. When the prompt says NO FIX IS AVAILABLE,
  propose mitigations only and never an upgrade or a version.
- A detection task is a normal task with severity (critical/high/medium/low), cves and a relevant: block that detects;
  its actions are the fix. boost: lists up to five hunt probes that only raise confidence, never decide.
- Containers: use restart_container / stop_container / start_container / update_container / container_rollback for one
  container, stack_restart / stack_update for a compose stack (project name), never run docker or podman via a script.
  Never write stack_deploy or stack_read yourself — Vigil builds them, because they carry one-time tickets.
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
        return Response(
            {"detail": f"{provider.name} is missing a model/URL"}, status=409
        )
    started = time.monotonic()
    try:
        text = provider_for(provider).complete(SYSTEM_PROMPT, prompt)
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
            "suggestions": _extract_suggestions(text),
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
        out.append(
            {
                "yaml": block.strip(),
                "parsed": spec,
                "risk": spec.get("derived_risk") or spec.get("risk", "standard"),
            }
        )
        if len(out) == 3:
            break
    return out


def _alert_prompt(alert) -> str:
    host = getattr(alert, "host", None)
    lines = [f"Alert: {alert}"]
    if host is not None:
        lines += [
            f"Host: {host.hostname}",
            f"OS: {host.os}",
            f"Tags: {', '.join(map(str, host.tags or []))}",
        ]
    for attr in ("message", "severity", "metric_value"):
        v = getattr(alert, attr, None)
        if v:
            lines.append(f"{attr.capitalize()}: {v}")
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
