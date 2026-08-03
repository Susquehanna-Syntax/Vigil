"""Agent side of remote reprovisioning. See docs/reprovisioning.md.

Staging is reversible: it writes files under /boot and nothing else. Commit
writes a ONE-SHOT bootloader entry — never a default change, and never kexec.
If the installer fails to boot, the entry lapses and the machine returns to
the old OS unaided, which is the property that makes this safe to run
remotely. Anything that would break that property refuses instead.
"""
import hashlib
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("vigil.agent.reprovision")

BOOT_DIR = Path("/boot")
STAGE_DIR = BOOT_DIR / "vigil-reprovision"
GRUB_D_ENTRY = Path("/etc/grub.d/42_vigil_reprovision")
ENTRY_ID = "vigil-reprovision"
ENTRY_TITLE = "Vigil Reprovision"

#: Installer RAM floors, in bytes, by family.
_MIN_RAM = {"ubuntu": 2 * 1024**3, "debian": 1 * 1024**3, "rhel": 2 * 1024**3}

#: Kernel + initrd plus headroom. Below this, staging would fill /boot.
_MIN_BOOT_FREE = 512 * 1024 * 1024

_CHUNK = 1024 * 1024


class PreflightError(Exception):
    """A precondition failed; nothing has been changed."""


def _run(cmd: list[str], timeout: int = 120) -> str:
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, check=True).stdout


def detect_bootloader() -> str:
    """Return 'grub2', 'systemd-boot', or 'unknown'.

    Unknown is reported, never guessed at: writing a boot entry for a
    bootloader we did not identify is how machines stop booting.
    """
    if (BOOT_DIR / "grub" / "grub.cfg").exists() or \
       (BOOT_DIR / "grub2" / "grub.cfg").exists():
        return "grub2"
    if (BOOT_DIR / "loader" / "entries").is_dir():
        return "systemd-boot"
    return "unknown"


def _list_disks() -> list[dict]:
    """Every block device with its size and current mount points."""
    try:
        out = _run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"],
                   timeout=30)
        data = json.loads(out)
    except Exception:
        logger.exception("lsblk failed")
        return []
    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        disks.append({
            "name": f"/dev/{dev['name']}",
            "size_bytes": int(dev.get("size") or 0),
            "mountpoints": [c.get("mountpoint") for c in dev.get("children", [])
                            if c.get("mountpoint")],
        })
    return disks


def _free_boot_bytes() -> int:
    st = os.statvfs(BOOT_DIR)
    return st.f_bavail * st.f_frsize


def _total_ram_bytes() -> int:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def preflight(params: dict, _config) -> str:
    """Report rebuild readiness. Read-only: changes nothing, ever."""
    target = params.get("disk_target") or ""
    family = params.get("os_family") or "ubuntu"
    disks = _list_disks()
    bootloader = detect_bootloader()

    failures = []
    if bootloader == "unknown":
        failures.append("Unrecognised bootloader — refusing to stage")
    if target and not any(d["name"] == target for d in disks):
        failures.append(f"Target disk {target} not present")

    free = _free_boot_bytes()
    if free < _MIN_BOOT_FREE:
        failures.append(f"/boot has only {free // 1024 // 1024} MiB free")

    ram = _total_ram_bytes()
    if ram < _MIN_RAM.get(family, 1024**3):
        failures.append(
            f"{ram // 1024**3} GiB RAM is below the {family} installer minimum")

    return json.dumps({
        "ok": not failures,
        "failures": failures,
        "bootloader": bootloader,
        # Every disk, not just the target: selecting the data disk by mistake
        # is caught by a human reading this list, not by a predicate.
        "disks": disks,
        "boot_free_bytes": free,
        "ram_bytes": ram,
        "architecture": os.uname().machine,
    })


def _download(url: str, dest: Path) -> None:
    import urllib.request

    with urllib.request.urlopen(url, timeout=600) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)


def _verify(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual.lower() != (expected or "").lower():
        raise PreflightError(
            f"{path.name} checksum mismatch: expected {expected}, got {actual}")


def stage(params: dict, _config) -> str:
    """Download and verify the installer kernel and initrd.

    Reversible: writes only into STAGE_DIR. On any failure the directory is
    removed, so a failed stage leaves the machine exactly as it was.
    """
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        for url_key, sha_key, name in (
            ("kernel_url", "kernel_sha256", "vmlinuz"),
            ("initrd_url", "initrd_sha256", "initrd"),
        ):
            dest = STAGE_DIR / name
            _download(params[url_key], dest)
            _verify(dest, params[sha_key])
    except Exception:
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
        raise
    return f"Staged installer for job {params.get('job_id')}"


def commit(params: dict, _config) -> str:
    """Write a ONE-SHOT boot entry and reboot.

    One-shot is the entire safety story: if the installer does not come up,
    the next boot returns to the old OS with no intervention.
    """
    cmdline = params["cmdline"]
    bootloader = detect_bootloader()
    if bootloader == "grub2":
        _write_grub_entry(cmdline)
    elif bootloader == "systemd-boot":
        _write_systemd_boot_entry(cmdline)
    else:
        raise PreflightError("Unrecognised bootloader — refusing to commit")

    subprocess.Popen(["systemctl", "reboot"])
    return f"Committed job {params.get('job_id')}; rebooting into installer"


def _write_grub_entry(cmdline: str) -> None:
    entry = (
        f"menuentry '{ENTRY_TITLE}' --id {ENTRY_ID} {{\n"
        f"    linux /vigil-reprovision/vmlinuz {cmdline}\n"
        "    initrd /vigil-reprovision/initrd\n"
        "}\n"
    )
    GRUB_D_ENTRY.write_text("#!/bin/sh\nexec cat <<'EOF'\n" + entry + "EOF\n")
    GRUB_D_ENTRY.chmod(0o755)

    for cmd in (["update-grub"],
                ["grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"]):
        if shutil.which(cmd[0]):
            _run(cmd)
            break

    for cmd in (["grub-reboot", ENTRY_ID], ["grub2-reboot", ENTRY_ID]):
        if shutil.which(cmd[0]):
            _run(cmd, timeout=60)
            return
    # Never fall back to set-default: that would make a failed installer boot
    # permanent, which is exactly what the one-shot design prevents.
    raise PreflightError(
        "No grub-reboot binary found — refusing to set a default entry")


def _write_systemd_boot_entry(cmdline: str) -> None:
    entries = BOOT_DIR / "loader" / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    (entries / f"{ENTRY_ID}.conf").write_text(
        f"title {ENTRY_TITLE}\n"
        "linux /vigil-reprovision/vmlinuz\n"
        "initrd /vigil-reprovision/initrd\n"
        f"options {cmdline}\n")
    _run(["bootctl", "set-oneshot", f"{ENTRY_ID}.conf"], timeout=60)


def cleanup(params: dict, _config) -> str:
    """Remove staged files. Idempotent — dispatched on abort, and on failure
    when the old OS is still reachable."""
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    GRUB_D_ENTRY.unlink(missing_ok=True)
    return f"Cleaned up job {params.get('job_id')}"
