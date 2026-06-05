"""Vulnerability scanner registry.

Vigil supports multiple vuln scanners behind a common ABC. Each impl decides
its own configuration source (env vars), launch model (REST poll, agent push,
…), and finding shape; the registry just gives the periodic sync task a list
of scanners to walk.

See SPEC_vuln_scanners.md at the repo root for the full design.
"""

from .base import Scanner
from .greenbone import GreenboneScanner
from .nessus import NessusScanner
from .trivy import TrivyScanner

# Keyed by Scanner.name. All three v1 scanners are registered;
# configured() decides which actually run during sync_vulns.
SCANNER_REGISTRY: dict[str, type[Scanner]] = {
    NessusScanner.name: NessusScanner,
    GreenboneScanner.name: GreenboneScanner,
    TrivyScanner.name: TrivyScanner,
}

__all__ = [
    "Scanner",
    "NessusScanner", "GreenboneScanner", "TrivyScanner",
    "SCANNER_REGISTRY",
]
