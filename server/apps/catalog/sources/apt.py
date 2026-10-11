"""Ubuntu and Debian: the ``Packages`` index of each release the fleet runs.

A release is "ubuntu:noble" or "debian:bookworm"; hosts are mapped to one
from their inventory (OS name + version) by ``release_for``.
"""
import re

from ..download import gunzip

SOURCE = "dpkg"
PLATFORM = "linux"

#: version → codename, for the releases apt still serves.
UBUNTU = {"20.04": "focal", "22.04": "jammy", "24.04": "noble", "24.10": "oracular",
          "25.04": "plucky", "25.10": "questing"}
DEBIAN = {"11": "bullseye", "12": "bookworm", "13": "trixie"}


def release_for(os_name: str, os_version: str) -> str:
    name, version = (os_name or "").lower(), (os_version or "").strip()
    if "ubuntu" in name:
        code = UBUNTU.get(".".join(version.split(".")[:2]))
        return f"ubuntu:{code}" if code else ""
    if "debian" in name:
        code = DEBIAN.get(version.split(".")[0])
        return f"debian:{code}" if code else ""
    return ""


def urls(release: str, arch: str = "amd64") -> list[str]:
    distro, code = release.split(":", 1)
    if distro == "ubuntu":
        base = ("https://archive.ubuntu.com/ubuntu" if arch in ("amd64", "i386")
                else "https://ports.ubuntu.com/ubuntu-ports")
        return [f"{base}/dists/{code}/{comp}/binary-{arch}/Packages.gz" for comp in ("main", "universe")]
    return [f"https://deb.debian.org/debian/dists/{code}/main/binary-{arch}/Packages.gz"]


#: Debian policy §5.6.7: what a package name may be.
_DEB_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")

#: Sections and name shapes that are libraries, headers, debug symbols or
#: data — there are tens of thousands and nobody shops for them.
_SKIP_SECTIONS = {"libs", "libdevel", "oldlibs", "debug", "localization", "doc", "fonts", "kernel"}
_SKIP_SUFFIXES = ("-dev", "-dbg", "-dbgsym", "-doc", "-data", "-common", "-l10n", "-locale")


def _not_an_app(pid: str, section: str) -> bool:
    section = section.split("/")[-1]
    return (section in _SKIP_SECTIONS or pid.startswith(("lib", "linux-", "language-pack"))
            or "-locale-" in pid or "-l10n-" in pid or "-langpack-" in pid
            or pid.endswith(_SKIP_SUFFIXES) or pid.startswith(("python3-", "golang-", "node-", "r-cran-",
                                                                "ruby-", "php-", "perl-")))


def parse(data: bytes, release: str = ""):
    text = gunzip(data).decode("utf-8", "replace")
    for stanza in text.split("\n\n"):
        fields, last = {}, None
        for line in stanza.split("\n"):
            if line.startswith((" ", "\t")) and last:
                fields[last] += "\n" + line[1:]
            elif ":" in line:
                last, _, value = line.partition(":")
                fields[last] = value.strip()
        pid = fields.get("Package")
        if not pid or not _DEB_NAME.match(pid) or _not_an_app(pid, fields.get("Section", "")):
            continue
        desc = fields.get("Description", "")
        summary, _, body = desc.partition("\n")
        body = "\n".join("" if ln.strip() == "." else ln.strip() for ln in body.split("\n")).strip()
        yield {
            "package_id": pid, "name": pid, "summary": summary[:300], "description": body[:8000],
            "version": fields.get("Version", "")[:120],
            "publisher": fields.get("Maintainer", "").split("<")[0].strip()[:200],
            "homepage": fields.get("Homepage", "")[:500],
            "categories": [fields["Section"].split("/")[-1]] if fields.get("Section") else [],
        }
