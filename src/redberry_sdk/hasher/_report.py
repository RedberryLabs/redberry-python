"""The signed hash report (VALP ``valp_collector.hashes``) and the optional pricing-sample upload,
as ``@redberrylabs/otel``'s ``report.ts`` sends them (SPEC.md §6)::

    POST {REDBERRY_URL}/v1/agents/{agent_id}/hashes
      authorization: Bearer <project token>       (64 lowercase hex)
      content-type: application/json
      x-redberry-key: <key_id>                     (32 lowercase hex)
      x-redberry-signature: ed25519=<base64>       (Ed25519 over 0x05 || SHA-256(raw body); 88 characters)
      body: canonical JSON of exactly {v, agent_id, trace_id, sha256, size, started_at, ended_at, stored,
            store, leaf_kind}

201 recorded (a new leaf), 200 the same body again (a retry), other 4xx final, 5xx / 429 / network
errors retried. A 200 or 201 reply that carries ``sample: {url, method, headers}`` asks for the stored
bytes, which go there with ``PUT`` and exactly those headers (``upload_sample``).
"""
from __future__ import annotations

import base64
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ._canonical import canonical_bytes
from ._crypto import Signer
from ._http import (
    DEFAULT_RETRY_DELAYS,
    Request,
    Transport,
    describe,
    send,
    trim_base,
    urllib_transport,
)

__all__ = [
    "MAX_REPORT_BYTES",
    "ReportOutcome",
    "Sample",
    "SampleOutcome",
    "report_body",
    "report_bytes",
    "send_report",
    "upload_sample",
]

MAX_REPORT_BYTES = 4096


def report_body(*, agent_id: str, trace_id: str, sha256: str, size: int, started_at: str, ended_at: str,
                stored: bool, store: str) -> dict[str, Any]:
    return {"v": 1, "agent_id": agent_id, "trace_id": trace_id, "sha256": sha256, "size": size,
            "started_at": started_at, "ended_at": ended_at, "stored": stored, "store": store,
            "leaf_kind": "stored-bytes"}


def report_bytes(body: dict[str, Any]) -> bytes:
    """The report's bytes: canonical JSON, so a retry sends exactly the same body (VALP answers 200, not 409)."""
    raw = canonical_bytes(body)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"hash report is over {MAX_REPORT_BYTES} bytes")
    return raw


@dataclass(frozen=True)
class Sample:
    url: str
    method: str = "PUT"
    headers: dict[str, str] | None = None
    object: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class ReportOutcome:
    outcome: Literal["recorded", "duplicate", "refused", "failed"]
    status: int | None
    attempts: int
    leaf: str | None = None
    window_start: str | None = None
    sample: Sample | None = None
    error: str | None = None


@dataclass(frozen=True)
class SampleOutcome:
    ok: bool
    status: int | None
    existing: bool = False
    error: str | None = None


def send_report(*, url: str, token: str, signer: Signer, body: dict[str, Any], transport: Transport | None = None,
                retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                sleep: Callable[[float], None] = time.sleep) -> ReportOutcome:
    raw = report_bytes(body)
    signature = base64.b64encode(signer.sign(raw)).decode("ascii")
    target = f"{trim_base(url)}/v1/agents/{urllib.parse.quote(body['agent_id'], safe='')}/hashes"
    req = Request("POST", target, {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "x-redberry-key": signer.key_id,
        "x-redberry-signature": f"ed25519={signature}",
    }, raw)
    r = send(transport or urllib_transport, req, retry_delays, sleep)
    if r.response is None:
        return ReportOutcome("failed", None, r.attempts, error=str(r.error))
    status = r.response.status
    if status in (200, 201):
        reply = r.response.json()
        reply = reply if isinstance(reply, dict) else {}
        raw_sample = reply.get("sample")
        sample = None
        if isinstance(raw_sample, dict) and isinstance(raw_sample.get("url"), str) and raw_sample["url"]:
            headers = raw_sample.get("headers")
            sample = Sample(url=raw_sample["url"], method=str(raw_sample.get("method") or "PUT"),
                            headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else None,
                            object=raw_sample.get("object"), expires_at=raw_sample.get("expires_at"))
        return ReportOutcome("recorded" if status == 201 else "duplicate", status, r.attempts,
                             leaf=reply.get("leaf") or None, window_start=reply.get("window_start") or None,
                             sample=sample)
    error = describe(r.response)
    if 400 <= status < 500 and status != 429:
        return ReportOutcome("refused", status, r.attempts, error=error)
    return ReportOutcome("failed", status, r.attempts, error=error)


def upload_sample(sample: Sample, data: bytes, transport: Transport | None = None,
                  retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                  sleep: Callable[[float], None] = time.sleep) -> SampleOutcome:
    """PUT the stored bytes to the signed pricing-sample URL VALP returned, with exactly its headers."""
    req = Request(sample.method or "PUT", sample.url,
                  dict(sample.headers) if sample.headers is not None else {"content-type": "application/gzip"}, data)
    r = send(transport or urllib_transport, req, retry_delays, sleep)
    if r.response is None:
        return SampleOutcome(False, None, error=str(r.error))
    if r.response.ok:
        return SampleOutcome(True, r.response.status)
    # Create-only (x-goog-if-generation-match: 0) and pinned to this size and hash: 412 means it is there.
    if r.response.status == 412:
        return SampleOutcome(True, 412, existing=True)
    return SampleOutcome(False, r.response.status, error=describe(r.response))
