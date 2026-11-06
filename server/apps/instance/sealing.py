"""Encrypt a backup archive before it leaves the machine (sprint M3 phase 4c).

A backup holds the database, the media and every key the install has, so an
off-site copy is sealed with a passphrase only the operator knows. The format
is a streaming AEAD (the STREAM construction): the passphrase is stretched with
scrypt over a random salt into an AES-256-GCM key, and the archive is sealed in
1 MiB chunks, each under its own nonce (a random 8-byte prefix and the chunk's
counter) with the header and a last-chunk flag as associated data. So a chunk
cannot be altered, reordered, dropped or appended, and the file cannot be cut
short, without decryption failing.

    magic(8) "VIGILENC" | version(1) | salt(16) | nonce prefix(8) | chunks...
    chunk: length(4, big-endian) | ciphertext (plaintext length + 16-byte tag)
"""
from __future__ import annotations

import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"VIGILENC"
VERSION = 1
CHUNK = 1024 * 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
_HEADER = len(MAGIC) + 1 + 16 + 8


class SealError(Exception):
    """The file is not a sealed backup, the passphrase is wrong, or it was
    altered. Deliberately one error: which of these it was is not told."""


def _key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise ValueError("a passphrase is required")
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(passphrase.encode())


def _nonce(prefix: bytes, counter: int) -> bytes:
    if counter >= 2 ** 32:
        raise ValueError("file too large to seal")
    return prefix + struct.pack(">I", counter)


def _aad(header: bytes, last: bool) -> bytes:
    return header + (b"\x01" if last else b"\x00")


def is_sealed(path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(len(MAGIC)) == MAGIC


def seal(src, dst, passphrase: str) -> None:
    salt, prefix = os.urandom(16), os.urandom(8)
    header = MAGIC + bytes([VERSION]) + salt + prefix
    aead = AESGCM(_key(passphrase, salt))
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(header)
        counter = 0
        chunk = fin.read(CHUNK)
        while True:
            following = fin.read(CHUNK)
            last = not following
            sealed = aead.encrypt(_nonce(prefix, counter), chunk, _aad(header, last))
            fout.write(struct.pack(">I", len(sealed)) + sealed)
            if last:
                break
            chunk, counter = following, counter + 1


def unseal(src, dst, passphrase: str) -> None:
    """Decrypt `src` into `dst`. On any failure `dst` is removed and SealError
    raised, so a half-written plaintext is never left behind as if it were the
    backup."""
    try:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            header = fin.read(_HEADER)
            if len(header) != _HEADER or header[:len(MAGIC)] != MAGIC or header[len(MAGIC)] != VERSION:
                raise SealError("not a sealed Vigil backup")
            salt = header[len(MAGIC) + 1:len(MAGIC) + 17]
            prefix = header[len(MAGIC) + 17:]
            aead = AESGCM(_key(passphrase, salt))
            counter = 0
            pending = fin.read(4)
            if len(pending) != 4:
                raise SealError("cut short")
            while True:
                (size,) = struct.unpack(">I", pending)
                if size > CHUNK + 16:
                    raise SealError("bad chunk")
                sealed = fin.read(size)
                if len(sealed) != size:
                    raise SealError("cut short")
                pending = fin.read(4)
                last = not pending
                try:
                    fout.write(aead.decrypt(_nonce(prefix, counter), sealed, _aad(header, last)))
                except InvalidTag:
                    raise SealError("wrong passphrase, or the file was altered") from None
                if last:
                    break
                if len(pending) != 4:
                    raise SealError("cut short")
                counter += 1
    except BaseException:
        try:
            os.unlink(dst)
        except OSError:
            pass
        raise
