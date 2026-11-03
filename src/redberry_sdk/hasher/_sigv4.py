"""AWS Signature Version 4, on the standard library (no AWS SDK).

Only what the hasher's S3 store needs: a request signed in the ``Authorization`` header, with the
payload's SHA-256 given (S3's ``x-amz-content-sha256``) or computed. Checked against AWS's published
test vectors (the ``aws-sig-v4-test-suite`` and the S3 single-chunk examples) in
``tests/unit/test_sdk_hasher_s3.py``.

https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["ALGORITHM", "EMPTY_SHA256", "Credentials", "canonical_request", "sign", "signing_key", "string_to_sign"]

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_UNRESERVED = "-_.~"


@dataclass(frozen=True)
class Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    #: Unix time the credentials expire (temporary credentials), or None.
    expires_at: float | None = None

    def __repr__(self) -> str:  # never the secret
        return f"Credentials(access_key_id={self.access_key_id!r}, session_token={'set' if self.session_token else None})"


def _quote(value: str, safe: str = "") -> str:
    return urllib.parse.quote(value, safe=_UNRESERVED + safe)


def _canonical_uri(path: str, double_encode: bool) -> str:
    """S3 (``double_encode=False``) encodes each segment once and keeps the path as given; every other
    service normalises it (``.``/``..``, repeated slashes) and encodes the encoded segments again."""
    path = path or "/"
    if not double_encode:
        return "/".join(_quote(urllib.parse.unquote(seg)) for seg in path.split("/"))
    segments: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segments:
                segments.pop()
            continue
        segments.append(seg)
    normal = "/" + "/".join(segments) + ("/" if path.endswith("/") and segments else "")
    return "/".join(_quote(_quote(urllib.parse.unquote(seg))) for seg in normal.split("/"))


def _canonical_query(query: str) -> str:
    pairs = []
    for part in query.split("&") if query else []:
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((_quote(urllib.parse.unquote_plus(key)), _quote(urllib.parse.unquote_plus(value))))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    rows: dict[str, list[str]] = {}
    for name, value in headers.items():
        rows.setdefault(name.strip().lower(), []).append(" ".join(str(value).strip().split()))
    names = sorted(rows)
    return "".join(f"{n}:{','.join(rows[n])}\n" for n in names), ";".join(names)


def canonical_request(method: str, url: str, headers: Mapping[str, str], payload_sha256: str, *,
                      double_encode: bool = True) -> tuple[str, str]:
    """The canonical request and its signed headers (every header given is signed)."""
    parts = urllib.parse.urlsplit(url)
    canonical_headers, signed = _canonical_headers(headers)
    text = "\n".join([method.upper(), _canonical_uri(parts.path, double_encode), _canonical_query(parts.query),
                      canonical_headers, signed, payload_sha256])
    return text, signed


def _scope(amz_date: str, region: str, service: str) -> str:
    return f"{amz_date[:8]}/{region}/{service}/aws4_request"


def string_to_sign(canonical: str, amz_date: str, region: str, service: str) -> str:
    return "\n".join([ALGORITHM, amz_date, _scope(amz_date, region, service),
                      hashlib.sha256(canonical.encode("utf-8")).hexdigest()])


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = ("AWS4" + secret).encode("utf-8")
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    return key


def amz_date(now: _dt.datetime | float | None = None) -> str:
    if now is None:
        now = _dt.datetime.now(_dt.UTC)
    elif isinstance(now, (int, float)):
        now = _dt.datetime.fromtimestamp(now, _dt.UTC)
    return now.astimezone(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def sign(method: str, url: str, headers: Mapping[str, str], body: bytes | None, credentials: Credentials, region: str,
         service: str, *, now: _dt.datetime | float | None = None, payload_sha256: str | None = None,
         double_encode: bool | None = None) -> dict[str, str]:
    """``headers`` plus ``host``, ``x-amz-date``, ``x-amz-security-token`` (temporary credentials) and
    ``authorization``. ``x-amz-date`` already in ``headers`` is kept (the test vectors fix it); S3 also
    wants ``x-amz-content-sha256``, which the caller adds. ``double_encode`` defaults to False for S3."""
    out = {k.lower(): str(v) for k, v in headers.items()}
    out.setdefault("host", urllib.parse.urlsplit(url).netloc)
    date = out.setdefault("x-amz-date", amz_date(now))
    if credentials.session_token:
        out["x-amz-security-token"] = credentials.session_token
    payload = payload_sha256 or hashlib.sha256(body or b"").hexdigest()
    canonical, signed = canonical_request(method, url, out, payload,
                                          double_encode=(service != "s3") if double_encode is None else double_encode)
    to_sign = string_to_sign(canonical, date, region, service)
    signature = hmac.new(signing_key(credentials.secret_access_key, date[:8], region, service),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out["authorization"] = (f"{ALGORITHM} Credential={credentials.access_key_id}/{_scope(date, region, service)}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out
