"""ISO import: verify, extract, discard.

pycdlib reads ISO9660 in pure Python, so no loop mount and no privileged
container. The ISO is deleted once the tree is unpacked (§6) — storage stays
at roughly 1x per image, and the digest recorded at import is the record that
the bytes were what they claimed to be.
"""
import hashlib
import logging
import os
import shutil
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("vigil.reprovision")

_CHUNK = 1024 * 1024

#: Where each family keeps its installer kernel and initrd inside the ISO.
#: Where each family keeps its installer kernel and initrd inside the ISO.
#:
#: Each entry is a list of CANDIDATES tried in order, because the layout drifts
#: between derivatives and between releases of the same distro. Ubuntu ships
#: /casper/initrd; Linux Mint, built on the same casper, ships initrd.lz. One
#: hardcoded path per family means a derivative fails extraction *after* a
#: multi-gigabyte download, which is the most expensive moment to discover a
#: filename.
KERNEL_PATHS = {
    "ubuntu": (
        ("/casper/vmlinuz", "/casper/initrd"),
        ("/casper/vmlinuz", "/casper/initrd.lz"),
        ("/casper/vmlinuz", "/casper/initrd.gz"),
    ),
    "debian": (
        ("/install.amd/vmlinuz", "/install.amd/initrd.gz"),
        ("/install.amd/vmlinuz", "/install.amd/initrd"),
    ),
    "rhel": (
        ("/images/pxeboot/vmlinuz", "/images/pxeboot/initrd.img"),
    ),
}


def _resolve_kernel_paths(iso, family: str) -> tuple[str, str]:
    """Pick the first candidate pair this ISO actually contains.

    ISO-9660 paths are case-sensitive and often carry a ``;1`` version suffix,
    so existence is probed by asking pycdlib for the record rather than by
    listing and matching strings.
    """
    try:
        candidates = KERNEL_PATHS[family]
    except KeyError:
        raise ImportError_(f"Unsupported family {family!r}")

    tried = []
    for kernel_src, initrd_src in candidates:
        tried.append(initrd_src)
        try:
            for path in (kernel_src, initrd_src):
                iso.get_record(iso_path=path)
        except Exception:  # noqa: BLE001 — pycdlib raises its own types
            continue
        return kernel_src, initrd_src

    raise ImportError_(
        f"No installer kernel/initrd found in this ISO for family {family!r}. "
        f"Tried: {', '.join(tried)}. The image may be a live-only or "
        f"non-installer variant, or a derivative with a layout Vigil does not "
        f"know yet.")


class ImportError_(Exception):
    """Import failed.

    Named with a trailing underscore deliberately: shadowing the builtin
    ImportError here would swallow genuine module-import failures.
    """


def image_root() -> Path:
    """Where extracted images live. Pure — creating it is the caller's job,
    inside their error handling, so a bad path cannot raise past a caller
    that promised not to."""
    return Path(getattr(settings, "VIGIL_IMAGE_ROOT", "/var/lib/vigil/images"))


def verify_checksum(path: Path, expected: str) -> None:
    """Raise unless *path* hashes to *expected*.

    Streams in 1 MiB chunks — these files run to several gigabytes. An empty
    *expected* is refused rather than treated as "no check wanted": an image
    registered without a digest must never import silently.
    """
    if not expected:
        raise ImportError_("No SHA-256 recorded for this image — refusing to import")
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        raise ImportError_(
            f"SHA-256 checksum mismatch: expected {expected}, got {actual}")


def import_iso(image, iso_path: Path) -> None:
    """Verify, extract kernel/initrd and the install tree, discard the ISO.

    Never raises: a broken import must not take the Celery worker down.
    Failures are recorded on the image, which lands on status FAILED and can
    therefore never be selected in the ceremony.
    """
    import pycdlib

    dest = None
    try:
        dest = image_root() / str(image.id)
        verify_checksum(iso_path, image.sha256)

        if image.os_family not in KERNEL_PATHS:
            raise ImportError_(f"Unsupported family {image.os_family!r}")

        dest.mkdir(parents=True, exist_ok=True)
        iso = pycdlib.PyCdlib()
        iso.open(str(iso_path))
        try:
            kernel_src, initrd_src = _resolve_kernel_paths(iso, image.os_family)
            for src, name in ((kernel_src, "vmlinuz"), (initrd_src, "initrd")):
                out = dest / name
                with out.open("wb") as fh:
                    iso.get_file_from_iso_fp(fh, iso_path=src)
            tree = dest / "tree"
            tree.mkdir(exist_ok=True)
            _extract_tree(iso, tree)
        finally:
            iso.close()

        size = iso_path.stat().st_size
        image.kernel_path = str(dest / "vmlinuz")
        image.initrd_path = str(dest / "initrd")
        image.tree_path = str(tree)
        image.size_bytes = size
        image.status = image.Status.READY
        image.import_error = ""
        image.save(update_fields=["kernel_path", "initrd_path", "tree_path",
                                  "size_bytes", "status", "import_error"])
        iso_path.unlink(missing_ok=True)
        logger.info("imported image %s (%s bytes)", image.id, size)
    except Exception as exc:
        logger.exception("ISO import failed for image %s", image.id)
        if dest is not None:
            shutil.rmtree(dest, ignore_errors=True)
        image.status = image.Status.FAILED
        image.import_error = str(exc)[:2000]
        image.save(update_fields=["status", "import_error"])


def _safe_join(root: Path, *parts: str) -> Path:
    """Join under *root*, refusing anything that escapes it.

    Path names come out of the ISO image, which is attacker-controlled input
    the moment anyone imports an image they did not build themselves. A
    crafted Rock Ridge or Joliet name containing ``..`` would otherwise let
    extraction write anywhere the Django user can reach — the ISO-Slip
    variant of Zip Slip. Absolute names are equally rejected.
    """
    root = root.resolve()
    target = root
    for part in parts:
        target = target / part.lstrip("/")
    target = Path(os.path.normpath(target))
    if target != root and root not in target.parents:
        raise ImportError_(
            f"ISO entry escapes the image directory: {'/'.join(parts)!r}")
    return target


def _extract_tree(iso, dest: Path) -> None:
    """Copy the whole ISO9660 tree to *dest*.

    The installer fetches this over HTTP as its package source, so the layout
    has to survive intact. ISO9660 version suffixes (``FOO.TXT;1``) are
    stripped — the installer asks for the plain name.
    """
    for dirname, _dirlist, filelist in iso.walk(iso_path="/"):
        target_dir = _safe_join(dest, dirname)
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in filelist:
            src = f"{dirname.rstrip('/')}/{filename}"
            out = _safe_join(dest, dirname, filename.split(";")[0])
            with out.open("wb") as fh:
                iso.get_file_from_iso_fp(fh, iso_path=src)
