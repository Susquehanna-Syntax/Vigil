"""Copy each backup to S3-compatible storage, sealed (sprint M3 phase 4c).

Any S3 API works (AWS, Cloudflare R2, Backblaze B2, MinIO, Wasabi): requests
are signed with AWS Signature Version 4 and addressed path-style
(``<endpoint>/<bucket>/<key>``), which every one of them accepts. Only a sealed
archive is ever uploaded (``sealing``): with no passphrase set there is no
upload at all, never a plaintext one.

Settings (Settings -> Retention & Limits, or the environment):
VIGIL_BACKUP_S3_URL, VIGIL_BACKUP_S3_BUCKET, VIGIL_BACKUP_S3_REGION,
VIGIL_BACKUP_S3_ACCESS_KEY, VIGIL_BACKUP_S3_SECRET_KEY, VIGIL_BACKUP_PASSPHRASE.
"""
from __future__ import annotations

import hashlib
import html
import hmac
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

UNSIGNED = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
TIMEOUT = (10, 600)
SEALED_RE = re.compile(r"^vigil-\d{8}T\d{6}Z\.tar\.gz\.enc$")


class OffsiteError(Exception):
    pass


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(method: str, url: str, *, region: str, access_key: str, secret_key: str,
         payload_hash: str = UNSIGNED, headers: dict | None = None, service: str = "s3",
         now: datetime | None = None) -> dict:
    """The headers that sign one request (SigV4). `url` carries the query
    string, if any; `headers` are extra headers to sign as well."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    parts = urlsplit(url)
    signed = {"host": parts.netloc, "x-amz-date": amz_date}
    if service == "s3":
        signed["x-amz-content-sha256"] = payload_hash
    for k, v in (headers or {}).items():
        signed[k.lower()] = v.strip()
    query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(_query_pairs(parts.query)))
    names = sorted(signed)
    canonical = "\n".join([
        method,
        quote(parts.path or "/", safe="/-_.~"),
        query,
        "".join(f"{n}:{signed[n]}\n" for n in names),
        ";".join(names),
        payload_hash,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in signed.items() if k != "host"}
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return out


def _query_pairs(query: str):
    from urllib.parse import unquote
    for part in filter(None, query.split("&")):
        k, _, v = part.partition("=")
        yield unquote(k), unquote(v)


class Bucket:
    def __init__(self, url, bucket, region, access_key, secret_key):
        if not re.fullmatch(r"https?://[^/\s]+/?", url or ""):
            raise OffsiteError("VIGIL_BACKUP_S3_URL must be a bare endpoint, e.g. https://s3.eu-west-1.amazonaws.com")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", bucket or ""):
            raise OffsiteError("VIGIL_BACKUP_S3_BUCKET is not a valid bucket name")
        self.base = url.rstrip("/") + "/" + bucket
        self.creds = dict(region=region or "us-east-1", access_key=access_key, secret_key=secret_key)

    def _url(self, key="", query=""):
        return self.base + ("/" + quote(key, safe="/-_.~") if key else "/") + (f"?{query}" if query else "")

    def put_file(self, key: str, path: Path):
        url = self._url(key)
        headers = sign("PUT", url, **self.creds)
        with open(path, "rb") as fh:
            r = requests.put(url, data=fh, headers={**headers, "Content-Length": str(path.stat().st_size)},
                             timeout=TIMEOUT)
        if r.status_code >= 300:
            raise OffsiteError(f"upload refused: HTTP {r.status_code} {r.text[:300]}")

    def list_keys(self, prefix="vigil-") -> list[str]:
        url = self._url(query=f"list-type=2&prefix={quote(prefix, safe='')}")
        r = requests.get(url, headers=sign("GET", url, payload_hash=EMPTY_SHA256, **self.creds), timeout=TIMEOUT)
        if r.status_code >= 300:
            raise OffsiteError(f"listing refused: HTTP {r.status_code} {r.text[:300]}")
        # The keys alone, read without an XML parser: nothing in this answer
        # needs one, and a parser is attack surface (entity expansion) for a
        # response from a host the operator typed in.
        return [html.unescape(k) for k in re.findall(r"<Key>([^<]{1,1024})</Key>", r.text)]

    def delete(self, key: str):
        url = self._url(key)
        r = requests.delete(url, headers=sign("DELETE", url, payload_hash=EMPTY_SHA256, **self.creds),
                            timeout=TIMEOUT)
        if r.status_code >= 300 and r.status_code != 404:
            raise OffsiteError(f"delete refused: HTTP {r.status_code} {r.text[:300]}")


def configured() -> bool:
    from .config import setting
    return bool(setting("VIGIL_BACKUP_S3_URL") and setting("VIGIL_BACKUP_S3_BUCKET"))


def copy_offsite(archive: Path, keep) -> str:
    """Seal `archive`, upload it, prune old sealed copies. Returns the key."""
    from . import sealing
    from .config import setting

    passphrase = setting("VIGIL_BACKUP_PASSPHRASE") or ""
    if not passphrase:
        raise OffsiteError("Set a backup passphrase before copying off-site: an archive holds every key "
                           "this install has, and it is never uploaded unencrypted.")
    bucket = Bucket(setting("VIGIL_BACKUP_S3_URL"), setting("VIGIL_BACKUP_S3_BUCKET"),
                    setting("VIGIL_BACKUP_S3_REGION"), setting("VIGIL_BACKUP_S3_ACCESS_KEY") or "",
                    setting("VIGIL_BACKUP_S3_SECRET_KEY") or "")
    sealed = archive.with_name(archive.name + ".enc.partial")
    key = archive.name + ".enc"
    try:
        sealing.seal(archive, sealed, passphrase)
        bucket.put_file(key, sealed)
    finally:
        sealed.unlink(missing_ok=True)
    if isinstance(keep, int) and keep >= 1:
        ours = sorted((k for k in bucket.list_keys() if SEALED_RE.match(k)), reverse=True)
        for old in ours[keep:]:
            bucket.delete(old)
    return key
