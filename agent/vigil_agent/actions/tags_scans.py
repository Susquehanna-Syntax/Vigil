"""Tag requests, Nessus / network scan requests, and agent-local Trivy scans.

Moved out of executor.py (which was 2,150 lines). Helpers that tests patch by
name on ``executor`` (``_run``, ``_docker_inspect``, ``_recreate_container``,
``_validate_path``) are called through ``ex.`` so those patches keep applying;
executor re-exports every name defined here.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import shutil

from .. import executor as ex
from ..config import AgentConfig
from ..executor import ActionOutput, _SAFE_IMAGE, logger


def _tag_names(params: dict) -> list[str]:
    """The tags named by an add_tag/remove_tag step.

    Accepts a list or a comma-separated string so a hand-written YAML
    definition can say either.
    """
    raw = params.get("tags", "")
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(t).strip() for t in (raw or []) if str(t).strip()]


def _add_tag(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server tags this host.

    No work happens here: tags are server-side metadata about the host, not
    state on it. The server applies the tags recorded in the signed task, so
    what lands is what an operator authorized — this output is a report, not
    an instruction.
    """
    tags = _tag_names(params)
    if not tags:
        raise ValueError("add_tag needs at least one tag")
    return ActionOutput(f"Tag requested: {', '.join(tags)} — server will apply",
                        {"tags": ", ".join(tags)})


def _remove_tag(params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server untags this host. See _add_tag."""
    tags = _tag_names(params)
    if not tags:
        raise ValueError("remove_tag needs at least one tag")
    return ActionOutput(f"Tag removal requested: {', '.join(tags)} — server will apply",
                        {"tags": ", ".join(tags)})


def _request_nessus_scan(_params: dict, _config: AgentConfig) -> str:
    """Emit a marker so the server records a Nessus scan request.

    No real work happens on the agent — Nessus scans the host's IP from
    the central scanner. The server inspects completed task params for
    this action and creates a ``VulnScan(state=REQUESTED)`` row, which
    the next ``sync_vulns`` cycle launches against Nessus.
    """
    return ActionOutput("Nessus scan requested — central scanner will pick it up",
                        {"requested": True})


def _request_network_scan(params: dict, _config: AgentConfig) -> str:
    """Engine-agnostic version of ``_request_nessus_scan``.

    The agent has no opinion about which network scanner runs — that's
    the server's call. We just emit a marker; the task-completion
    handler decides Nessus vs. Greenbone based on
    ``params.engine`` (if set) or the host's preferred_scanners.
    """
    engine = (params.get("engine") or "auto").strip()
    return ActionOutput(f"Network scan requested (engine={engine}) — server will dispatch",
                        {"engine": engine})


# Trivy actions ─────────────────────────────────────────────────────────────
# Trivy is agent-local: the scan runs here and we ship the JSON back as
# task output. The server's task-completion handler routes the JSON into
# apps/vulns/scanners/trivy.py:TrivyScanner.ingest_report.

_TRIVY_SCOPE_PATTERN = re.compile(r"^(fs|rootfs|image:[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,254})$")


# Subprocess wall-clock budget for a scan, and Trivy's own internal scan
# deadline kept just under it. Trivy's *default* --timeout is 5m, which
# routinely expires while walking a real root filesystem and surfaces as
# "semaphore acquire: context deadline exceeded" — so we set it explicitly.
_TRIVY_SUBPROCESS_TIMEOUT = 1200
_TRIVY_SCAN_TIMEOUT = _TRIVY_SUBPROCESS_TIMEOUT - 60

# Directories full of large content-addressed blobs (flatpak/docker/containers/
# snap) that aren't OS or language package sources. Walking them adds minutes
# and yields no findings — and analysing those blobs is what stalls the scan.
_TRIVY_SKIP_DIRS = (
    "/var/lib/flatpak",
    "/var/lib/docker",
    "/var/lib/containers",
    "/var/lib/snapd",
    "/var/snap",
)

# The only per-vulnerability fields the server reads — see
# apps/vulns/scanners/trivy.py:TrivyScanner.ingest_report. Everything else
# Trivy attaches to a finding (Description, References, CVSS, DataSource,
# Layer, PkgIdentifier) is prose that no part of Vigil ever looks at.
_TRIVY_VULN_FIELDS = (
    "VulnerabilityID",
    "PkgName",
    "Severity",
    "Title",
    "InstalledVersion",
    "FixedVersion",
)

# Per-result keys carrying bulk we never ingest. Packages is the big one: on a
# stock Ubuntu workstation it was 21 MB of a 23.5 MB report — the full SBOM
# inventory, listed alongside the 2.4 MB of findings we actually want.
_TRIVY_RESULT_BULK = ("Packages", "Secrets", "Misconfigurations", "Licenses")

def _condense_trivy_report(raw: str) -> str:
    """Strip a Trivy report down to what the server ingests.

    A real ``trivy fs /`` report is enormous — 30,159,056 characters measured
    on a stock Ubuntu workstation, 379 results and 803 vulnerabilities. The
    transport caps task output, so what reached the server was an unterminated
    fragment of JSON and no scan was ever ingested. Condensing here takes that
    same report to roughly 243,000 characters, which fits with room to spare.

    Two properties the server depends on are preserved exactly:

      * **every** result survives, even ones with no findings — the SBOM
        refusal (#18) reasons over the whole ``Results`` list, and dropping
        the empty ones would turn "this scan never looked for
        vulnerabilities" into "nothing was scanned";
      * the *presence or absence* of each result's ``Vulnerabilities`` key is
        untouched. An empty list means a clean host; a missing key means the
        vulnerability scanner never ran. Conflating those marks every real
        finding fixed and reports the host clean (#19).

    Anything we cannot parse is returned exactly as it came. The server's
    diagnostics for a broken report are better than a guess made here, and
    ``_run`` hands back stdout and stderr concatenated, so a Trivy WARN line
    can sit on either side of the JSON — that text is kept as-is.
    """
    text = raw or ""
    start = text.find("{")
    if start == -1:
        return text

    decoder = json.JSONDecoder()
    while start != -1:
        try:
            data, end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(data, dict) and "Results" in data:
            break
        start = text.find("{", start + 1)
    else:
        return text

    condensed = json.dumps(_slim_report(data), separators=(",", ":"))

    # Splice back in place so any surrounding stderr text is left intact.
    return text[:start] + _pack_report(condensed) + text[end:]


#: Prefix marking a gzipped, base64-encoded report. The server keys off this to
#: decide whether to decompress; anything without it is read as plain JSON, so
#: an agent from before compression still ingests fine.
TRIVY_GZIP_MARKER = "[TRIVY-GZ]"


def _pack_report(report_json: str) -> str:
    """Compress a condensed report for the wire.

    A Trivy report is JSON with the same handful of keys repeated once per
    finding, which is close to the ideal case for gzip: 6.6x on a real report,
    5.0x after base64. That is what keeps a large host inside a single
    request — the alternative was raising the request-body ceiling and
    shedding fields, both of which cost more than they bought.

    Compression is best-effort. If anything here fails, the plain report is
    still correct and still ingestible; only the size advantage is lost.
    """
    try:
        packed = TRIVY_GZIP_MARKER + base64.b64encode(
            gzip.compress(report_json.encode("utf-8"), 6)).decode("ascii")
    except Exception:  # noqa: BLE001
        logger.warning("Could not compress the Trivy report; sending it plain")
        return report_json
    # Refuse to make things worse. A tiny report can come out larger once
    # base64 has added its third, and a plain report is easier to diagnose.
    return packed if len(packed) < len(report_json) else report_json


def _slim_report(data: dict) -> dict:
    """Rebuild a Trivy report carrying only what the server ingests.

    Deduplicates on ``(PkgName, VulnerabilityID)`` — the exact key the server
    reconciles on. The same package/CVE is reported once per binary that links
    it, 3.0x duplication on a real report, and every copy after the first is
    transferred only to be collapsed by ``update_or_create`` at the far end.
    The last occurrence wins, because that is the one whose values a host ends
    up with today.

    Every result survives even when dedup empties it, and each result's
    ``Vulnerabilities`` key keeps its presence or absence: an empty list means
    a clean target, a missing key means the vulnerability scanner never ran,
    and the server refuses the second rather than marking everything fixed.
    """
    # (pkg, cve) -> (index of the result it last appeared in, slimmed entry)
    latest: dict[tuple, tuple[int, dict]] = {}
    for i, result in enumerate(data.get("Results") or []):
        if not isinstance(result, dict):
            continue
        for vuln in (result.get("Vulnerabilities") or []):
            if not isinstance(vuln, dict):
                continue
            key = (vuln.get("PkgName"), vuln.get("VulnerabilityID"))
            latest[key] = (
                i, {f: vuln[f] for f in _TRIVY_VULN_FIELDS if vuln.get(f) is not None})

    kept: dict[int, list] = {}
    for index, entry in latest.values():
        kept.setdefault(index, []).append(entry)

    condensed = {k: v for k, v in data.items() if k != "Results"}
    results = []
    for i, result in enumerate(data.get("Results") or []):
        if not isinstance(result, dict):
            results.append(result)
            continue
        slim = {k: v for k, v in result.items()
                if k not in _TRIVY_RESULT_BULK and k != "Vulnerabilities"}
        if "Vulnerabilities" in result:
            slim["Vulnerabilities"] = kept.get(i, [])
        results.append(slim)
    condensed["Results"] = results
    return condensed


def _run_trivy_scan(params: dict, _config: AgentConfig) -> str:
    """Run ``trivy`` against the local filesystem or a named image.

    ``scope`` selects what to scan:
      * ``fs`` (default) — ``trivy fs /``
      * ``rootfs`` — alias for ``fs``
      * ``image:<name>`` — ``trivy image <name>``

    Returns the JSON report, condensed to the fields the server ingests (see
    :func:`_condense_trivy_report`). No local interpretation happens — no
    finding is judged or dropped here — but the SBOM package inventory and
    the per-CVE prose are stripped, because a full report is ~30 MB and does
    not survive the trip.

    The scan is restricted to the ``vuln`` scanner: a default ``fs`` scan also
    runs the *secret* scanner, which reads and analyses every file on disk.
    That's both wasted work (the server only ingests vulnerabilities) and the
    usual cause of stalls on hosts with large blob stores. Combined with an
    explicit ``--timeout`` and a skip-list, scans complete reliably.
    """
    if shutil.which("trivy") is None:
        raise RuntimeError(
            "trivy binary not found in PATH — install it with "
            "'curl -sSL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh' "
            "or run the 'Install Trivy' task template"
        )

    scope = (params.get("scope") or "fs").strip()
    if not _TRIVY_SCOPE_PATTERN.match(scope):
        raise ValueError(f"Invalid trivy scope: {scope!r}")

    common = [
        "--quiet",
        "--format", "json",
        "--severity", "CRITICAL,HIGH,MEDIUM,LOW",
        "--scanners", "vuln",
        "--timeout", f"{_TRIVY_SCAN_TIMEOUT}s",
    ]
    if scope in ("fs", "rootfs"):
        skip = []
        for d in _TRIVY_SKIP_DIRS:
            # Under `trivy fs /` the walker matches paths *relative* to the
            # scan root (e.g. "var/lib/flatpak/…", no leading slash), so an
            # absolute --skip-dirs may not match. Pass both forms to be safe.
            skip += ["--skip-dirs", d, "--skip-dirs", d.lstrip("/")]
        cmd = ["trivy", "fs", *common, *skip, "/"]
    else:
        # scope = "image:<name>"
        image_name = scope[len("image:"):]
        if not _SAFE_IMAGE.match(image_name):
            raise ValueError(f"Invalid image name in trivy scope: {image_name!r}")
        cmd = ["trivy", "image", *common, image_name]

    text = _condense_trivy_report(ex._run(cmd, timeout=_TRIVY_SUBPROCESS_TIMEOUT))
    return ActionOutput(text, {"vulnerabilities": _count_trivy_vulnerabilities(text)})


def _count_trivy_vulnerabilities(text: str) -> int:
    """Total length of every Vulnerabilities list in a Trivy report.

    Uses the same raw_decode scan ``_condense_trivy_report`` uses to locate the
    ``Results`` object, so surrounding stderr text and a compressed report both
    behave the same way. Returns -1 when the text cannot be parsed as a report.
    """
    text = text or ""
    marker = TRIVY_GZIP_MARKER
    if marker in text:
        body = text[text.index(marker) + len(marker):]
        try:
            text = gzip.decompress(base64.b64decode(body)).decode("utf-8")
        except (ValueError, OSError, UnicodeDecodeError):
            return -1
    start = text.find("{")
    if start == -1:
        return -1

    decoder = json.JSONDecoder()
    while start != -1:
        try:
            data, _ = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(data, dict) and "Results" in data:
            total = 0
            for result in data["Results"] or []:
                if isinstance(result, dict):
                    total += len(result.get("Vulnerabilities") or [])
            return total
        start = text.find("{", start + 1)
    return -1


def _trivy_db_update(_params: dict, _config: AgentConfig) -> str:
    """Force a refresh of Trivy's local vulnerability database.

    Trivy auto-updates on first scan but caches between runs; this
    action is for explicit refreshes (e.g. after a security advisory).
    """
    if shutil.which("trivy") is None:
        raise RuntimeError("trivy binary not found in PATH")
    text = ex._run(["trivy", "--quiet", "image", "--download-db-only"], timeout=300)
    return ActionOutput(text, {"updated": True})
