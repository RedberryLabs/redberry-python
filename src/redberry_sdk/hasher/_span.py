"""``ReadableSpan`` -> the span object of ``@redberrylabs/otel`` SPEC.md §2 (OTLP/JSON field names,
nanosecond times as decimal strings), plus the ordering and classification the processor needs.

A ``SpanJson`` is a plain dict with every field of SPEC §2.1 always present::

    traceId, spanId, parentSpanId, name, kind, startTimeUnixNano, endTimeUnixNano, attributes, status,
    events, links, scope, dropped

Attributes are kept exactly as emitted (a JSON-text value stays a string); OpenTelemetry's tuples
are written as arrays.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

__all__ = ["SpanJson", "is_ai_span", "ns_to_iso", "sort_spans", "span_id_hex", "span_to_json", "trace_id_hex"]

SpanJson = dict[str, Any]


def trace_id_hex(trace_id: int) -> str:
    return format(trace_id, "032x")


def span_id_hex(span_id: int) -> str:
    return format(span_id, "016x")


def ns_to_iso(ns: int | str) -> str:
    """Nanoseconds since the epoch -> ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (UTC, milliseconds, truncated)."""
    seconds, ms = divmod(int(ns) // 1_000_000, 1000)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def _attrs(a: Any) -> dict[str, Any]:
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in dict(a or {}).items()}


def _scope(span: Any) -> dict[str, Any]:
    scope = getattr(span, "instrumentation_scope", None) or getattr(span, "instrumentation_info", None)
    if scope is None:
        return {"name": "", "version": None, "schemaUrl": None}
    return {"name": scope.name or "", "version": scope.version or None,
            "schemaUrl": getattr(scope, "schema_url", None) or None}


def parent_span_id(span: Any) -> str | None:
    parent = getattr(span, "parent", None)
    return span_id_hex(parent.span_id) if parent is not None and parent.span_id else None


def span_to_json(span: Any, open: bool = False) -> SpanJson:
    """SPEC §2.1 for one span. ``open``: the span had not ended when the trace was cut short."""
    sc = span.get_span_context() if hasattr(span, "get_span_context") else span.context
    status = span.status
    message = getattr(status, "description", None)
    return {
        "traceId": trace_id_hex(sc.trace_id),
        "spanId": span_id_hex(sc.span_id),
        "parentSpanId": parent_span_id(span),
        "name": span.name,
        "kind": int(getattr(span.kind, "value", span.kind)),
        "startTimeUnixNano": str(int(span.start_time)),
        "endTimeUnixNano": None if open or span.end_time is None else str(int(span.end_time)),
        "attributes": _attrs(span.attributes),
        "status": {"code": int(getattr(status.status_code, "value", status.status_code)),
                   "message": message or None},
        "events": [{"name": e.name, "timeUnixNano": str(int(e.timestamp)), "attributes": _attrs(e.attributes),
                    "droppedAttributesCount": int(getattr(e, "dropped_attributes", 0) or 0)}
                   for e in span.events],
        "links": [{"traceId": trace_id_hex(link.context.trace_id), "spanId": span_id_hex(link.context.span_id),
                   "attributes": _attrs(link.attributes),
                   "droppedAttributesCount": int(getattr(link, "dropped_attributes", 0) or 0)}
                  for link in span.links],
        "scope": _scope(span),
        "dropped": {"attributes": int(getattr(span, "dropped_attributes", 0) or 0),
                    "events": int(getattr(span, "dropped_events", 0) or 0),
                    "links": int(getattr(span, "dropped_links", 0) or 0)},
    }


def sort_spans(spans: list[SpanJson]) -> list[SpanJson]:
    """SPEC §2.2: by start time (as integers), then span id in ordinal order."""
    return sorted(spans, key=lambda s: (int(s["startTimeUnixNano"]), s["spanId"]))


#: Attribute keys (or prefixes) that mark a model or agent span in the dialects the SDK's
#: instrumentors emit: OTel GenAI, OpenInference and Traceloop.
_AI_KEYS = ("gen_ai.operation.name", "gen_ai.system", "gen_ai.provider.name", "openinference.span.kind",
            "llm.request.type")


def is_ai_span(s: SpanJson) -> bool:
    """A model or agent span. SPEC §8's AI SDK rule (``gen_ai.operation.name``, a name starting ``ai.``,
    scope ``gen_ai`` or ``ai``), widened to the Python instrumentors: ``gen_ai.system`` /
    ``gen_ai.provider.name`` (OTel GenAI), ``openinference.span.kind`` (OpenInference, the SDK's
    Anthropic instrumentor) and ``llm.request.type`` (Traceloop)."""
    attrs = s["attributes"]
    return (s["name"].startswith("ai.") or s["scope"]["name"] in ("ai", "gen_ai")
            or any(k in attrs for k in _AI_KEYS))
