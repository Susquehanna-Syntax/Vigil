"""Distro images Vigil can fetch on its own.

Data, not code and not database rows: a new distro release is a one-line
change with no migration. Entries go stale as distros release — accepted, and
the custom-URL path covers the gap until this file is updated.

Only images with a stable anonymous URL and a published SHA-256 belong here.
RHEL proper needs a Red Hat subscription and Microsoft publishes no stable
hashed URL, so both reach the library through upload instead. A catalog entry
that cannot actually fetch fails at the worst possible moment — after the
operator has chosen it.

Every digest below was verified against the distro's own published checksum
file on 2026-08-07 (see task-1-report.md for the exact source URL and
fetched line for each entry). Re-verify against the same kind of source
before changing any digest here.
"""
from __future__ import annotations

CATALOG: tuple[dict, ...] = (
    {
        "id": "ubuntu-24.04-server-amd64",
        "name": "Ubuntu Server 24.04 LTS",
        "os_family": "ubuntu",
        "version": "24.04",
        "architecture": "x86_64",
        "url": "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso",
        "sha256": "e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433",
        "size_bytes": 3405469696,
    },
    {
        "id": "ubuntu-22.04-server-amd64",
        "name": "Ubuntu Server 22.04 LTS",
        "os_family": "ubuntu",
        "version": "22.04",
        "architecture": "x86_64",
        "url": "https://releases.ubuntu.com/22.04/ubuntu-22.04.5-live-server-amd64.iso",
        "sha256": "9bc6028870aef3f74f4e16b900008179e78b130e6b0b9a140635434a46aa98b0",
        "size_bytes": 2136926208,
    },
    {
        "id": "debian-13-netinst-amd64",
        "name": "Debian 13 (netinst)",
        "os_family": "debian",
        "version": "13",
        "architecture": "x86_64",
        "url": "https://cdimage.debian.org/debian-cd/13.6.0/amd64/iso-cd/debian-13.6.0-amd64-netinst.iso",
        "sha256": "65273beed27b2df543b68b65630ba525cfbad8df2b12035732b2dff87d6664e7",
        "size_bytes": 791674880,
    },
    {
        "id": "rocky-9-minimal-x86_64",
        "name": "Rocky Linux 9 (minimal)",
        "os_family": "rhel",
        "version": "9",
        "architecture": "x86_64",
        "url": "https://download.rockylinux.org/pub/rocky/9/isos/x86_64/Rocky-9.8-x86_64-minimal.iso",
        "sha256": "d338032cd1cdd41c67139f2f71b4c832c8e4a21943106519db9c7137df7a63d4",
        "size_bytes": 2755067904,
    },
    {
        "id": "almalinux-9-minimal-x86_64",
        "name": "AlmaLinux 9 (minimal)",
        "os_family": "rhel",
        "version": "9",
        "architecture": "x86_64",
        "url": "https://repo.almalinux.org/almalinux/9/isos/x86_64/AlmaLinux-9.8-x86_64-minimal.iso",
        "sha256": "7762a4b45a66235726db145a573658964bf77bf7b9bc1c018afe86a4cf37cc2e",
        "size_bytes": 2838495232,
    },
)


def get_entry(entry_id: str) -> dict | None:
    """Return the catalog entry with this id, or None."""
    for entry in CATALOG:
        if entry["id"] == entry_id:
            return entry
    return None
