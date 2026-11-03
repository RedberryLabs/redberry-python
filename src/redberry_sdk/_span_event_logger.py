"""Bridge: converts OTel log records (GenAI events) → span.add_event() calls.

The opentelemetry-instrumentation-openai-v2 ≥ 2.x emits message content via
the OTel Logs API (Logger.emit / LogRecord) rather than span.add_event().
Without a configured LoggerProvider these records are silently dropped.

This module provides a lightweight LoggerProvider that intercepts each emit()
call and re-emits it as a span event on the currently active span so that
the normaliser's existing span-event parser continues to work unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

from opentelemetry import trace
from opentelemetry._logs import Logger, LoggerProvider, LogRecord
from opentelemetry.context import Context


class _SpanEventLogger(Logger):
    """Converts each LogRecord into a span.add_event() on the active span."""

    # Abstract on Logger since opentelemetry-api 1.4x: without it this class cannot be
    # instantiated, openai-v2's instrument() raises, and OpenAI calls go unrecorded.
    def enabled(
        self,
        *,
        context: Context | None = None,
        severity_number=None,
        event_name: str | None = None,
    ) -> bool:
        return True

    def emit(
        self,
        record: LogRecord | None = None,
        *,
        timestamp: int | None = None,
        observed_timestamp: int | None = None,
        context: Context | None = None,
        severity_number=None,
        severity_text: str | None = None,
        body: Any = None,
        attributes=None,
        event_name: str | None = None,
        exception: BaseException | None = None,
    ) -> None:
        if record is not None:
            name = record.event_name or "gen_ai.event"
            raw_body = record.body
            raw_attrs = record.attributes or {}
        else:
            name = event_name or "gen_ai.event"
            raw_body = body
            raw_attrs = attributes or {}

        # Build flat string-valued attributes from the body dict + attrs.
        event_attrs: dict[str, Any] = {}
        if isinstance(raw_body, dict):
            for k, v in raw_body.items():
                if isinstance(v, (str, int, float, bool)):
                    event_attrs[k] = v
                else:
                    import json as _json
                    try:
                        event_attrs[k] = _json.dumps(v)
                    except Exception:  # noqa: BLE001
                        event_attrs[k] = str(v)
        elif raw_body is not None:
            event_attrs["body"] = str(raw_body)

        for k, v in raw_attrs.items():
            if isinstance(v, (str, int, float, bool)):
                event_attrs[k] = v

        # For streaming responses, the OpenAI v2 StreamWrapper passes the
        # span's context explicitly in the LogRecord so events go to the right
        # span even after the 'with start_as_current_span' block has exited.
        ctx = record.context if (record is not None and record.context is not None) else None
        span = trace.get_current_span(ctx)
        if span and span.is_recording():
            span.add_event(name, attributes=event_attrs)


class SpanEventLoggerProvider(LoggerProvider):
    """A LoggerProvider that always returns a _SpanEventLogger."""

    def get_logger(
        self,
        name: str,
        version: Optional[str] = None,
        schema_url: Optional[str] = None,
        attributes=None,
    ) -> Logger:
        return _SpanEventLogger(name=name, version=version, schema_url=schema_url)
