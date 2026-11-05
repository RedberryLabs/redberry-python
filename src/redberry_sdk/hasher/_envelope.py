"""The stored trace document (``@redberrylabs/otel`` SPEC.md §1) and its bytes, hash, size and path (§5)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from .._version import __version__
from ._canonical import canonical_bytes, deterministic_gzip
from ._span import SpanJson, ns_to_iso, sort_spans

__all__ = [
    "AGENT_ATTRIBUTE",
    "PRODUCER",
    "SCHEMA",
    "CutReason",
    "EncodedTrace",
    "Finish",
    "build_envelope",
    "encode_trace",
    "storage_path",
]

SCHEMA = "redberry.trace.v1"
#: Every stored span carries the agent the hasher resolved under this name (SPEC §2.1).
AGENT_ATTRIBUTE = "redberry.agent_id"
#: This writer, in every document's ``producer`` (SPEC §1). ``@redberrylabs/otel`` writes its own name.
PRODUCER = {"name": "redberry-sdk", "version": __version__}

Finish = Literal["complete", "cut_short"]
CutReason = Literal["deadline", "hard_stop", "shutdown"]


def build_envelope(*, agent_id: str, trace_id: str, finish: Finish, spans: list[SpanJson],
                   resource: dict[str, Any] | None = None, cut_reason: CutReason | None = None,
                   cut_at_ms: float | None = None, dropped_spans: int = 0,
                   producer: dict[str, str] | None = None) -> dict[str, Any]:
    """SPEC §1. ``cut_at_ms``: when a ``cut_short`` trace was cut (ms since the epoch); ``ended_at`` is
    at least that."""
    if not spans:
        raise ValueError("a trace has at least one span")
    ordered = [{**s, "attributes": {**s["attributes"], AGENT_ATTRIBUTE: agent_id}} for s in sort_spans(spans)]
    start = end = int(ordered[0]["startTimeUnixNano"])
    for s in ordered:
        st = int(s["startTimeUnixNano"])
        start = min(start, st)
        end = max(end, st if s["endTimeUnixNano"] is None else int(s["endTimeUnixNano"]))
    if cut_at_ms is not None:
        end = max(end, int(cut_at_ms // 1) * 1_000_000)
    return {
        "schema": SCHEMA,
        "agent_id": agent_id,
        "trace_id": trace_id,
        "finish": finish,
        "cut_reason": None if finish == "complete" else cut_reason,
        "started_at": ns_to_iso(start),
        "ended_at": ns_to_iso(end),
        "resource": {k: (list(v) if isinstance(v, tuple) else v) for k, v in dict(resource or {}).items()},
        "spans": ordered,
        "dropped_spans": dropped_spans,
        "producer": dict(producer or PRODUCER),
    }


@dataclass(frozen=True)
class EncodedTrace:
    #: Canonical JSON (SPEC §3), UTF-8.
    json: bytes
    #: The stored bytes: gzip of ``json`` (SPEC §4; see ``_canonical``).
    gz: bytes
    #: Lowercase hex SHA-256 of ``gz``: what the hash report commits to.
    sha256: str
    size: int


def encode_trace(envelope: dict[str, Any]) -> EncodedTrace:
    raw = canonical_bytes(envelope)
    gz = deterministic_gzip(raw)
    return EncodedTrace(json=raw, gz=gz, sha256=hashlib.sha256(gz).hexdigest(), size=len(gz))


def storage_path(envelope: dict[str, Any]) -> str:
    """SPEC §5: ``redberry/<agent_id>/<YYYY-MM-DD of started_at>/<trace_id>.json.gz``."""
    return f"redberry/{envelope['agent_id']}/{envelope['started_at'][:10]}/{envelope['trace_id']}.json.gz"
