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
#:
#: Each entry is a list of CANDIDATES tried in order, because the layout drifts
#: between derivatives and between releases of the same distro. Ubuntu ships
#: /casper/initrd; Linux Mint, built on the same casper, ships initrd.lz. One
#: hardcoded path per family means a derivative fails extraction *after* a
#: multi-gigabyte download, which is the most expensive moment to discover a
#: filename.
#:
#: These are Rock Ridge / Joliet names — what the file is actually called.
#: See _find_record for why that distinction is load-bearing.
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


def _iso9660_names(path: str) -> list[str]:
    """The same *path* as bare ISO-9660 would spell it.

    ISO-9660 proper uppercases every name and appends a ``;1`` version suffix,
    adding a bare ``.`` to names that have no extension: ``/casper/vmlinuz``
    is stored as ``/CASPER/VMLINUZ.;1``. Only reached on an ISO carrying
    neither Rock Ridge nor Joliet, which no mainstream distro ships.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return []
    *dirs, name = (p.upper() for p in parts)
    prefix = "/" + "".join(f"{d}/" for d in dirs)
    stem = name if "." in name else f"{name}."
    return [f"{prefix}{stem};1", f"{prefix}{name}"]


def _find_record(iso, path: str) -> dict | None:
    """Locate *path*, returning the pycdlib keyword that found it.

    An ISO carries the same file under up to three naming schemes, and
    pycdlib makes you say which one you are asking about. ``iso_path`` is the
    bare ISO-9660 namespace, where ``/casper/vmlinuz`` is spelled
    ``/CASPER/VMLINUZ.;1`` — so looking up a lowercase path there misses on
    *every* ISO ever made, which is precisely the bug this replaced: import
    failed on every image with a message blaming the distro's layout.

    Rock Ridge is asked first because it is the namespace whose names are the
    real ones, Joliet second, and munged ISO-9660 last. The winning keyword
    is returned rather than just True so extraction reads through the same
    namespace the probe succeeded in.
    """
    candidates: list[dict] = []
    for probe, keyword in ((iso.has_rock_ridge, "rr_path"),
                           (iso.has_joliet, "joliet_path")):
        try:
            if probe():
                candidates.append({keyword: path})
        except Exception:  # noqa: BLE001 — pycdlib raises its own types
            pass
    candidates += [{"iso_path": name} for name in _iso9660_names(path)]

    for lookup in candidates:
        try:
            iso.get_record(**lookup)
        except Exception:  # noqa: BLE001 — pycdlib raises its own types
            continue
        return lookup
    return None


def _resolve_kernel_paths(iso, family: str) -> tuple[dict, dict]:
    """Pick the first candidate pair this ISO actually contains.

    Returns the two pycdlib lookups that reach them — see _find_record.
    """
    try:
        candidates = KERNEL_PATHS[family]
    except KeyError:
        raise ImportError_(f"Unsupported family {family!r}")

    tried = []
    for kernel_src, initrd_src in candidates:
        tried.append(f"{kernel_src} + {initrd_src}")
        kernel = _find_record(iso, kernel_src)
        initrd = _find_record(iso, initrd_src)
        if kernel and initrd:
            return kernel, initrd

    raise ImportError_(
        f"No installer kernel/initrd found in this ISO for family {family!r}. "
        f"Tried: {'; '.join(tried)}. The image may be a live-only or "
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
            kernel_lookup, initrd_lookup = _resolve_kernel_paths(
                iso, image.os_family)
            for lookup, name in ((kernel_lookup, "vmlinuz"),
                                 (initrd_lookup, "initrd")):
                out = dest / name
                with out.open("wb") as fh:
                    iso.get_file_from_iso_fp(fh, **lookup)
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


def _tree_namespace(iso) -> str:
    """The pycdlib keyword whose names are the ones the installer asks for.

    The bare ISO-9660 namespace uppercases and version-suffixes everything,
    so walking it yields ``/CASPER/VMLINUZ.;1`` — and stripping the ``;1``
    leaves ``VMLINUZ.``, a name with a trailing dot that matches nothing the
    installer requests. Extraction has to follow Rock Ridge or Joliet, where
    the names are the real ones, or the tree serves 404s for every file
    despite having imported cleanly.
    """
    for probe, keyword in ((iso.has_rock_ridge, "rr_path"),
                           (iso.has_joliet, "joliet_path")):
        try:
            if probe():
                return keyword
        except Exception:  # noqa: BLE001 — pycdlib raises its own types
            pass
    return "iso_path"


def _symlink_target(iso, keyword: str, path: str) -> str | None:
    """The link target if *path* is a symlink, else None.

    Only Rock Ridge records symlinks — they are invisible in the bare
    ISO-9660 and Joliet namespaces, which is why extraction never met one
    until the walk moved to Rock Ridge.
    """
    if keyword != "rr_path":
        return None
    try:
        record = iso.get_record(**{keyword: path})
        if not record.is_symlink():
            return None
        target = record.rock_ridge.symlink_path()
    except Exception:  # noqa: BLE001 — pycdlib raises its own types
        return None
    if isinstance(target, bytes):
        target = target.decode("utf-8", "replace")
    return target or None


def _place_symlink(link: Path, target: str, root: Path) -> bool:
    """Recreate a symlink under *root*, or decline it. True if created.

    Two kinds are declined. A target that escapes the tree is the symlink
    form of the traversal `_safe_join` already refuses for names — an ISO is
    attacker-controlled input, and a link is a name that resolves later.

    A target at or above its own directory is declined because it is a loop:
    Ubuntu ships ``/ubuntu -> .`` as a URL alias, and reproducing it gives
    the extracted tree an infinite depth for anything that walks it. The
    installer is served paths Vigil generates, so the alias costs nothing.
    """
    root = root.resolve()
    resolved = Path(os.path.normpath(link.parent / target))
    if resolved != root and root not in resolved.parents:
        logger.warning("skipping symlink escaping the tree: %s -> %s",
                       link, target)
        return False
    if resolved == link.parent or resolved in link.parent.parents:
        logger.info("skipping self-referential symlink: %s -> %s", link, target)
        return False
    link.symlink_to(target)
    return True


def _extract_tree(iso, dest: Path) -> None:
    """Copy the whole ISO tree to *dest*.

    The installer fetches this over HTTP as its package source, so the layout
    has to survive intact. ISO9660 version suffixes (``FOO.TXT;1``) are
    stripped — the installer asks for the plain name.

    Symlinks are relinked rather than read: pycdlib refuses to hand over
    contents for one ("Symlinks have no data associated with them"), and a
    real distro ISO is full of them — Debian's ``dists/stable`` points at the
    codename directory the installer actually needs.
    """
    keyword = _tree_namespace(iso)
    for dirname, _dirlist, filelist in iso.walk(**{keyword: "/"}):
        target_dir = _safe_join(dest, dirname)
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in filelist:
            src = f"{dirname.rstrip('/')}/{filename}"
            out = _safe_join(dest, dirname, filename.split(";")[0])
            link_target = _symlink_target(iso, keyword, src)
            if link_target is not None:
                _place_symlink(out, link_target, dest)
                continue
            with out.open("wb") as fh:
                iso.get_file_from_iso_fp(fh, **{keyword: src})
