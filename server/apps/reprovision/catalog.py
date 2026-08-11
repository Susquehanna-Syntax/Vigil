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
        "id": "ubuntu-26.04-server-amd64",
        "name": "Ubuntu Server 26.04 LTS",
        "os_family": "ubuntu",
        "version": "26.04",
        "architecture": "x86_64",
        "url": "https://releases.ubuntu.com/26.04/ubuntu-26.04-live-server-amd64.iso",
        "sha256": "dec49008a71f6098d0bcfc822021f4d042d5f2db279e4d75bdd981304f1ca5d9",
        "size_bytes": 2918598656,
    },
    {
        "id": "ubuntu-26.04-desktop-amd64",
        "name": "Ubuntu Desktop 26.04 LTS",
        "os_family": "ubuntu",
        "version": "26.04",
        "architecture": "x86_64",
        "url": "https://releases.ubuntu.com/26.04/ubuntu-26.04-desktop-amd64.iso",
        "sha256": "487f87faaf547ea30e0aba4d5b53346292571256b25333a978db1692bcee9dd2",
        "size_bytes": 6518974464,
    },
    {
        "id": "fedora-43-server-x86_64",
        "name": "Fedora Server 43",
        "os_family": "rhel",
        "version": "43",
        "architecture": "x86_64",
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/43/Server/x86_64/iso/Fedora-Server-dvd-x86_64-43-1.6.iso",
        "sha256": "aca06983bef83da9b43144c1a2ff4c8483e4745167c17f53725c16a16742e643",
        "size_bytes": 3484483584,
    },
    {
        "id": "fedora-43-workstation-x86_64",
        "name": "Fedora Workstation 43",
        "os_family": "rhel",
        "version": "43",
        "architecture": "x86_64",
        "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/43/Workstation/x86_64/iso/Fedora-Workstation-Live-43-1.6.x86_64.iso",
        "sha256": "2a4a16c009244eb5ab2198700eb04103793b62407e8596f30a3e0cc8ac294d77",
        "size_bytes": 2742190080,
    },
    {
        "id": "linuxmint-22.3-cinnamon-64bit",
        "name": "Linux Mint 22.3 Cinnamon",
        "os_family": "ubuntu",
        "version": "22.3",
        "architecture": "x86_64",
        "url": "https://mirrors.edge.kernel.org/linuxmint/stable/22.3/linuxmint-22.3-cinnamon-64bit.iso",
        "sha256": "a081ab202cfda17f6924128dbd2de8b63518ac0531bcfe3f1a1b88097c459bd4",
        "size_bytes": 3091660800,
    },
    {
        # Bazzite publishes a rolling "stable" URL rather than a versioned one,
        # so this digest goes stale on their next build while the URL keeps
        # working — the download succeeds and the checksum fails. That is a
        # noisier staleness than the others here, which 404 instead. Re-fetch
        # from https://download.bazzite.gg/bazzite-stable-amd64.iso-CHECKSUM
        # when it starts failing.
        "id": "bazzite-stable-amd64",
        "name": "Bazzite (stable)",
        "os_family": "rhel",
        "version": "stable",
        "architecture": "x86_64",
        "url": "https://download.bazzite.gg/bazzite-stable-amd64.iso",
        "sha256": "9c8d06cd8e57f2274678edeb14b4b13a79b8117c70571a65199919a66305b5c7",
        "size_bytes": 7907770368,
    },
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
