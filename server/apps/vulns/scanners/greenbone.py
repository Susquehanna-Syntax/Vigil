"""Greenbone Community Edition / OpenVAS scanner.

Talks GMP (Greenbone Management Protocol) — XML messages over a TLS
socket — to a Greenbone CE instance the user runs themselves. We keep
this self-contained: no python-gvm dependency, just stdlib ``ssl``,
``socket``, and ``xml.etree.ElementTree``. The protocol is request /
response over a single long-lived TLS connection per session, opened
fresh on each :meth:`sync` cycle.

Lifecycle parallels Nessus:

  * launch_pending → create_target → create_task → start_task
    (stores the GMP task UUID in ``VulnScan.external_scan_id``)
  * poll_active → get_tasks → map Greenbone status to ``VulnScan.State``
  * ingest_results → get_results → write :class:`VulnFinding` rows

Configuration via env vars surfaced through settings:

  * ``GREENBONE_URL`` — ``host:port`` of the GMP listener. The default
    Greenbone CE container exposes GMP on ``9390``.
  * ``GREENBONE_USERNAME`` / ``GREENBONE_PASSWORD`` — GMP credentials.
  * ``GREENBONE_VERIFY_SSL`` — set to ``false`` for self-signed certs.

This implementation hasn't been field-tested against a live Greenbone
instance yet. The GMP shapes used are documented in Greenbone's
`reference docs <https://docs.greenbone.net/API/GMP/gmp-22.4.html>`_;
the response parsing is conservative (returns sensible defaults on
missing attributes) so a version-skewed response degrades gracefully
into "scan still running" rather than crashing the periodic sync.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import xml.etree.ElementTree as ET
from typing import ClassVar

from django.conf import settings
from django.utils.timezone import now

from ..models import VulnFinding, VulnScan
from ..scoring import recompute_summary
from .base import Scanner

logger = logging.getLogger(__name__)


# Greenbone status string → our VulnScan.State.
_GMP_STATUS_MAP = {
    "New": VulnScan.State.LAUNCHED,
    "Requested": VulnScan.State.LAUNCHED,
    "Queued": VulnScan.State.LAUNCHED,
    "Running": VulnScan.State.RUNNING,
    "Done": VulnScan.State.COMPLETED,
    "Stopped": VulnScan.State.ABORTED,
    "Stop Requested": VulnScan.State.ABORTED,
    "Interrupted": VulnScan.State.FAILED,
    "Internal Error": VulnScan.State.FAILED,
}


# Greenbone "Full and fast" scan config — well-known UUID identical
# across installs. Saves a get_configs round-trip on every launch.
_FULL_AND_FAST_CONFIG_UUID = "daba56c8-73ec-11df-a475-002264764cea"

# Greenbone "OpenVAS Default" scanner UUID — also well-known.
_OPENVAS_DEFAULT_SCANNER_UUID = "08b69003-5fc2-4037-a479-93b440211c73"


# Severity float (CVSS) → our enum. Greenbone reports CVSS v2-style.
def _cvss_to_severity(cvss: float) -> str:
    if cvss >= 9.0:
        return VulnFinding.Severity.CRITICAL
    if cvss >= 7.0:
        return VulnFinding.Severity.HIGH
    if cvss >= 4.0:
        return VulnFinding.Severity.MEDIUM
    if cvss > 0:
        return VulnFinding.Severity.LOW
    return VulnFinding.Severity.INFO


# Hard ceiling on a single GMP response. get_results is paginated (see
# _ingest_task_results) so anything past this is a runaway, not data.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class _GmpClient:
    """Tiny GMP-over-TLS client used by :class:`GreenboneScanner`.

    One client = one TLS connection = one authenticated session. Each
    ``send()`` writes an XML element and reads until the response root
    element closes. Big result sets are paginated via ``first``/``rows``
    filter params so we never need to stream a multi-MB response in one
    shot, and a hard byte ceiling guards against runaways.
    """

    def __init__(self, host: str, port: int, verify_ssl: bool):
        ctx = ssl.create_default_context()
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=30)
        self._sock = ctx.wrap_socket(raw, server_hostname=host)

    def authenticate(self, username: str, password: str) -> None:
        resp = self.send(
            f"<authenticate><credentials>"
            f"<username>{_xml_escape(username)}</username>"
            f"<password>{_xml_escape(password)}</password>"
            f"</credentials></authenticate>"
        )
        if resp.get("status") != "200":
            raise RuntimeError(
                f"Greenbone auth failed: {resp.get('status')} {resp.get('status_text')}"
            )

    def send(self, xml_str: str) -> ET.Element:
        """Write one GMP command, read+parse the matching response."""
        self._sock.sendall(xml_str.encode("utf-8"))
        buf = bytearray()
        root_close: bytes | None = None
        # Read until the root element closes. Parsing the whole buffer
        # on every chunk is O(n²), so we first do a cheap tail check:
        # only attempt the parse when the buffer plausibly ends with
        # `</root>` (or `/>` for an empty-element response).
        while True:
            chunk = self._sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"Greenbone response exceeded {_MAX_RESPONSE_BYTES} bytes"
                )
            if root_close is None:
                # A DTD would have to appear before the root element.
                # GMP never sends one, and rejecting it outright closes
                # the XXE / entity-expansion class of XML attacks
                # without pulling in defusedxml.
                if b"<!DOCTYPE" in bytes(buf[:1024]):
                    raise RuntimeError(
                        "Greenbone response contains a DTD — refusing to parse"
                    )
                m = re.match(rb"\s*<([A-Za-z_][\w.-]*)", bytes(buf[:256]))
                if m:
                    root_close = b"</" + m.group(1) + b">"
            tail = bytes(buf[-4096:]).rstrip()
            if (
                root_close is not None
                and not tail.endswith(root_close)
                and not tail.endswith(b"/>")
            ):
                continue
            try:
                return ET.fromstring(bytes(buf))
            except ET.ParseError:
                continue
        raise RuntimeError("Greenbone connection closed before complete response")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&apos;")
    )


class GreenboneScanner(Scanner):
    name: ClassVar[str] = "greenbone"

    def configured(self) -> bool:
        return all([
            getattr(settings, "GREENBONE_URL", ""),
            getattr(settings, "GREENBONE_USERNAME", ""),
            getattr(settings, "GREENBONE_PASSWORD", ""),
        ])

    def sync(self) -> str:
        if not self.configured():
            return "not configured"
        try:
            host, port = _parse_gmp_url(settings.GREENBONE_URL)
        except ValueError as exc:
            return f"bad GREENBONE_URL: {exc}"

        verify = getattr(settings, "GREENBONE_VERIFY_SSL", True)
        try:
            client = _GmpClient(host, port, verify)
        except Exception as exc:
            logger.error("Greenbone: connection to %s:%s failed: %s", host, port, exc)
            return f"connect failed: {exc}"

        try:
            client.authenticate(
                settings.GREENBONE_USERNAME, settings.GREENBONE_PASSWORD,
            )
        except Exception as exc:
            client.close()
            return f"auth failed: {exc}"

        try:
            launched = self._launch_pending(client)
            polled = self._poll_active(client)
            ingested = self._ingest_completed(client)
            return (
                f"launched={launched} polled={polled} "
                f"hosts_synced={ingested}"
            )
        finally:
            client.close()

    # ----- phase helpers --------------------------------------------------

    def _launch_pending(self, client: _GmpClient) -> int:
        """For each REQUESTED Greenbone VulnScan: create target, task, start."""
        pending = list(
            VulnScan.objects.filter(
                state=VulnScan.State.REQUESTED,
                scanner=self.name,
            ).select_related("host")
        )
        if not pending:
            return 0

        launched = 0
        for scan in pending:
            target = (scan.target or scan.host.ip_address or "").strip()
            if not target:
                scan.state = VulnScan.State.FAILED
                scan.error = "Host has no IP address to scan"
                scan.finished_at = now()
                scan.save(update_fields=["state", "error", "finished_at"])
                continue
            try:
                # Names carry the VulnScan UUID — GVM requires unique
                # target names, so a bare "Vigil: <hostname>" would fail
                # with "Target exists already" on every re-scan.
                label = f"{scan.host.hostname} [{scan.id}]"
                target_id = self._create_target(client, label, target)
                task_id = self._create_task(client, label, target_id)
                self._start_task(client, task_id)

                scan.state = VulnScan.State.LAUNCHED
                scan.external_scan_id = task_id
                scan.launched_at = now()
                scan.save(update_fields=["state", "external_scan_id", "launched_at"])
                launched += 1
                logger.info(
                    "Greenbone: launched task %s for %s",
                    task_id, scan.host.hostname,
                )
            except Exception as exc:
                scan.state = VulnScan.State.FAILED
                scan.error = f"Greenbone launch failed: {exc}"[:1000]
                scan.finished_at = now()
                scan.save(update_fields=["state", "error", "finished_at"])
                logger.warning(
                    "Greenbone: launch for %s failed: %s", scan.host.hostname, exc,
                )
        return launched

    def _poll_active(self, client: _GmpClient) -> int:
        """Update local state for Greenbone scans still in flight."""
        active = list(
            VulnScan.objects.filter(
                state__in=[VulnScan.State.LAUNCHED, VulnScan.State.RUNNING],
                scanner=self.name,
            ).exclude(external_scan_id="")
        )
        polled = 0
        for scan in active:
            try:
                resp = client.send(
                    f'<get_tasks task_id="{_xml_escape(scan.external_scan_id)}"/>'
                )
                task_el = resp.find(".//task")
                if task_el is None:
                    continue
                status_el = task_el.find("status")
                status = status_el.text if status_el is not None else ""
                mapped = _GMP_STATUS_MAP.get(status, VulnScan.State.RUNNING)
                if mapped != scan.state:
                    scan.state = mapped
                    if mapped in {
                        VulnScan.State.COMPLETED,
                        VulnScan.State.FAILED,
                        VulnScan.State.ABORTED,
                    }:
                        scan.finished_at = now()
                    scan.save(update_fields=["state", "finished_at"])
                polled += 1
            except Exception as exc:
                logger.warning(
                    "Greenbone: poll for task %s failed: %s",
                    scan.external_scan_id, exc,
                )
        return polled

    def _ingest_completed(self, client: _GmpClient) -> int:
        """Ingest COMPLETED Greenbone scans exactly once.

        ``ingested_at`` gates the work: each completed scan is pulled a
        single time, oldest first, so when a host has several completed
        scans the newest one is ingested last and its result set is the
        one that decides which findings are OPEN vs FIXED.
        """
        completed = list(
            VulnScan.objects.filter(
                state=VulnScan.State.COMPLETED,
                scanner=self.name,
                ingested_at__isnull=True,
            )
            .exclude(external_scan_id="")
            .select_related("host")
            .order_by("finished_at")[:25]
        )
        synced = 0
        for scan in completed:
            try:
                self._ingest_task_results(client, scan)
                scan.ingested_at = now()
                scan.save(update_fields=["ingested_at"])
                synced += 1
            except Exception as exc:
                logger.warning(
                    "Greenbone: ingest for task %s failed: %s",
                    scan.external_scan_id, exc,
                )
        return synced

    # ----- GMP request helpers --------------------------------------------

    def _create_target(self, client: _GmpClient, label: str, ip: str) -> str:
        # Hosts list = single IP for now. Greenbone accepts comma-separated.
        resp = client.send(
            f'<create_target>'
            f'<name>Vigil: {_xml_escape(label)}</name>'
            f'<hosts>{_xml_escape(ip)}</hosts>'
            f'</create_target>'
        )
        if resp.get("status") != "201":
            raise RuntimeError(
                f"create_target: {resp.get('status')} {resp.get('status_text')}"
            )
        return resp.get("id") or ""

    def _create_task(self, client: _GmpClient, label: str, target_id: str) -> str:
        resp = client.send(
            f'<create_task>'
            f'<name>Vigil: {_xml_escape(label)}</name>'
            f'<config id="{_FULL_AND_FAST_CONFIG_UUID}"/>'
            f'<target id="{_xml_escape(target_id)}"/>'
            f'<scanner id="{_OPENVAS_DEFAULT_SCANNER_UUID}"/>'
            f'</create_task>'
        )
        if resp.get("status") != "201":
            raise RuntimeError(
                f"create_task: {resp.get('status')} {resp.get('status_text')}"
            )
        return resp.get("id") or ""

    def _start_task(self, client: _GmpClient, task_id: str) -> None:
        resp = client.send(f'<start_task task_id="{_xml_escape(task_id)}"/>')
        if resp.get("status") not in ("202",):
            raise RuntimeError(
                f"start_task: {resp.get('status')} {resp.get('status_text')}"
            )

    # Page size for get_results. GMP supports first/rows pagination in
    # the filter string; paging keeps individual responses small instead
    # of streaming one giant rows=-1 document.
    _RESULTS_PAGE_SIZE = 500

    def _ingest_task_results(self, client: _GmpClient, scan: VulnScan) -> None:
        """Pull results for one completed Greenbone task, write findings."""
        seen_keys: set[str] = set()
        host = scan.host

        first = 1
        while True:
            resp = client.send(
                f'<get_results filter="task_id={_xml_escape(scan.external_scan_id)} '
                f'first={first} rows={self._RESULTS_PAGE_SIZE} '
                f'levels=hmlg apply_overrides=1"/>'
            )
            results = resp.findall(".//result")

            for result in results:
                nvt_el = result.find("nvt")
                if nvt_el is None:
                    continue
                oid = nvt_el.get("oid") or ""
                if not oid:
                    continue
                name = (nvt_el.findtext("name") or "")[:255]

                cve_text = (nvt_el.findtext("cve") or "").strip()
                # GMP returns CVE as comma-separated or "NOCVE". Take first.
                cve_id = ""
                if cve_text and cve_text != "NOCVE":
                    cve_id = cve_text.split(",")[0].strip()[:32]

                try:
                    cvss = float((result.findtext("severity") or "0").strip())
                except ValueError:
                    cvss = 0.0
                severity = _cvss_to_severity(cvss)

                VulnFinding.objects.update_or_create(
                    host=host,
                    scanner=VulnScan.Scanner.GREENBONE,
                    plugin_id_or_oid=oid,
                    defaults={
                        "severity": severity,
                        "cve_id": cve_id,
                        "title": name,
                        "state": VulnFinding.State.OPEN,
                        "resolved_at": None,
                    },
                )
                seen_keys.add(oid)

            if len(results) < self._RESULTS_PAGE_SIZE:
                break
            first += self._RESULTS_PAGE_SIZE

        stale = VulnFinding.objects.filter(
            host=host,
            scanner=VulnScan.Scanner.GREENBONE,
            state=VulnFinding.State.OPEN,
        ).exclude(plugin_id_or_oid__in=seen_keys)
        if stale.exists():
            stale.update(state=VulnFinding.State.FIXED, resolved_at=now())

        recompute_summary(host)


def _parse_gmp_url(url: str) -> tuple[str, int]:
    """Parse ``host:port`` (or just ``host``) into a (host, port) tuple.

    Default port is 9390 (Greenbone CE GMP listener). IPv6 literals use
    the bracketed form: ``[::1]:9390`` or just ``[::1]``.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("empty URL")
    # Allow a scheme prefix even though GMP isn't HTTP — users might
    # type "tls://gvm:9390" or "gvm:9390" or "gvm". Strip the scheme.
    if "://" in url:
        url = url.split("://", 1)[1]
    if url.startswith("["):
        host, sep, rest = url[1:].partition("]")
        if not sep:
            raise ValueError("unclosed '[' in IPv6 address")
        if rest.startswith(":"):
            return host, int(rest[1:])
        return host, 9390
    if url.count(":") > 1:
        # Bare IPv6 literal with no port — every colon is part of the
        # address.
        return url, 9390
    if ":" in url:
        host, port_s = url.rsplit(":", 1)
        return host, int(port_s)
    return url, 9390
