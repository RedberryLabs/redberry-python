"""Recording the tools a request COULD have called, not just the one it did.

No upstream instrumentor captures the request's tool list — the one every
OpenAI-backed agent here runs on has no reference to `tools` at all. The
absence then travels the whole way down without ever announcing itself:
`available_tools_json` is None, `tool_schema` is None, SD1B serialises `"{}"`,
and CTA scores against no schema. Downstream that reads as "this tool takes no
arguments" rather than "nobody captured it", and a control that validates
arguments against a schema has nothing to validate against.

The reader always existed: `_otel_genai_v2` parses a `gen_ai.request.tools`
event. Only the writer was missing.
"""
from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from redberry_sdk.instrumentors._tool_definitions import (
    MAX_TOOLS_JSON_BYTES, TOOLS_EVENT, record_tools_on_current_span,
)

TOOLS = [{"type": "function", "function": {
    "name": "transfer_funds",
    "parameters": {"type": "object",
                   "properties": {"account_id": {"type": "string"}},
                   "required": ["account_id"]}}}]


@pytest.fixture
def tracing():
    """A provider per test. The GLOBAL provider can only be set once per
    process, so setting it here would silently give every test after the first
    the first one's exporter."""
    provider = TracerProvider()
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    return provider.get_tracer("test"), exp


def _record(tracing, **kwargs):
    tracer, exporter = tracing
    with tracer.start_as_current_span("chat"):
        recorded = record_tools_on_current_span(kwargs)
    return recorded, exporter.get_finished_spans()[-1]


def _tools_event(span):
    return next((e for e in span.events if e.name == TOOLS_EVENT), None)


def test_the_request_tool_list_lands_on_the_span(tracing):
    recorded, span = _record(tracing, tools=TOOLS)
    assert recorded
    event = _tools_event(span)
    assert event is not None
    assert json.loads(event.attributes["tools"]) == TOOLS


def test_the_schema_survives_the_round_trip(tracing):
    """What downstream actually needs is `required` and the property types —
    without them an argument validator cannot fire."""
    _recorded, span = _record(tracing, tools=TOOLS)
    params = json.loads(_tools_event(span).attributes["tools"])[0]["function"]["parameters"]
    assert params["required"] == ["account_id"]
    assert params["properties"]["account_id"]["type"] == "string"


def test_tool_choice_is_carried_when_given(tracing):
    _recorded, span = _record(tracing, tools=TOOLS, tool_choice="auto")
    assert _tools_event(span).attributes["tool_choice"] == "auto"


def test_a_request_with_no_tools_records_nothing(tracing):
    """Not a gap: a call that passed no tools genuinely had none."""
    recorded, span = _record(tracing, messages=[])
    assert recorded is False and _tools_event(span) is None


def test_an_oversized_tool_list_is_dropped_not_truncated(tracing):
    """A clipped list still reads as the complete set of tools, which is worse
    than recording none — it would make a real tool look undeclared."""
    huge = [{"type": "function", "function": {
        "name": f"tool_{i}", "description": "x" * 400,
        "parameters": {"type": "object"}}}
        for i in range(MAX_TOOLS_JSON_BYTES // 300)]
    recorded, span = _record(tracing, tools=huge)
    event = _tools_event(span)
    # No list at all — and a marker saying why, so the normaliser can report a
    # TRUNCATED gap rather than "the tool list never reached the span".
    assert recorded is False and "tools" not in event.attributes
    assert event.attributes["omitted"] == "size"
    assert event.attributes["tools_bytes"] > MAX_TOOLS_JSON_BYTES


def test_unserialisable_tools_do_not_raise(tracing):
    """A capture gap must never take down the agent being observed."""
    recorded, span = _record(tracing, tools=[{"fn": object()}])
    assert isinstance(recorded, bool)


def test_no_current_span_is_survivable():
    """Distinct from 'no tools were passed': only this one is a defect, and it
    must not throw either way."""
    assert record_tools_on_current_span({"tools": TOOLS}) in (True, False)
