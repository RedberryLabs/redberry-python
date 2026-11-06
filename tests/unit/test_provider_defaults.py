"""Anthropic API 2026-10-21 fixes sampling at temperature 1.0 and rejects any temperature sent.
The SDK records the API version a call used and, on 2026-10-21 or later with no temperature sent,
temperature 1.0 tagged ``provider_fixed_default``. 2023-06-01 is unchanged.

The SDK tests run the real anthropic client against an in-process transport, inside a
model-call span, and check the recorded version against the header the request carried.
"""
from __future__ import annotations

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from redberry_sdk.instrumentors import _provider_defaults as pd
from redberry_sdk.instrumentors._tool_definitions import enable_tool_definition_capture

NEW, OLD = "2026-10-21", "2023-06-01"


# ── the SDK: which version, and what it fixes ──────────────────────────────────────────────────

class _Client:
    """Stands in for a client whose class name and module are set per test."""

    def __init__(self, headers=None, base_url="https://api.anthropic.com", custom=None):
        self._headers, self.base_url, self._custom_headers = headers, base_url, custom or {}

    @property
    def default_headers(self):
        if self._headers is None:
            raise RuntimeError("unreadable")
        return self._headers


def _client(name="Anthropic", module="anthropic._client", **kw):
    cls = type(name, (_Client,), {"__module__": module})
    return cls(**kw)


class _Resource:
    def __init__(self, client):
        self._client = client


def _attrs(client, **kwargs):
    return pd.provider_default_attributes(_Resource(client), kwargs)


def test_new_version_without_temperature_is_inferred_from_the_header():
    a = _attrs(_client(headers={"anthropic-version": NEW}), model="m", messages=[])
    assert a == {pd.ATTR_API_VERSION: NEW, pd.ATTR_API_VERSION_SOURCE: "request_header",
                 pd.ATTR_TEMPERATURE_INFERRED: 1.0, pd.ATTR_TEMPERATURE_SOURCE: "provider_fixed_default"}


@pytest.mark.parametrize("junk", ["beta", "latest", "2026-10-21-preview", "3000"])
def test_a_version_that_is_not_an_iso_date_infers_nothing(junk):
    a = _attrs(_client(headers={"anthropic-version": junk}))
    assert a == {pd.ATTR_API_VERSION: junk, pd.ATTR_API_VERSION_SOURCE: "request_header"}


def test_a_later_version_is_inferred_too():
    a = _attrs(_client(headers={"anthropic-version": "2027-03-01"}))
    assert a[pd.ATTR_TEMPERATURE_SOURCE] == "provider_fixed_default"


def test_old_version_records_the_version_and_infers_nothing():
    a = _attrs(_client(headers={"anthropic-version": OLD}), model="m", messages=[])
    assert a == {pd.ATTR_API_VERSION: OLD, pd.ATTR_API_VERSION_SOURCE: "request_header"}


@pytest.mark.parametrize("sent", [{"temperature": 1.0}, {"extra_body": {"temperature": 0.2}}])
def test_a_temperature_the_request_sent_is_never_inferred_over(sent):
    a = _attrs(_client(headers={"anthropic-version": NEW}), **sent)
    assert pd.ATTR_TEMPERATURE_SOURCE not in a and a[pd.ATTR_API_VERSION] == NEW


def test_the_calls_extra_headers_override_the_client():
    client = _client(headers={"anthropic-version": NEW})
    assert _attrs(client, extra_headers={"Anthropic-Version": OLD}) == {
        pd.ATTR_API_VERSION: OLD, pd.ATTR_API_VERSION_SOURCE: "request_header"}
    client = _client(headers={"anthropic-version": OLD})
    assert _attrs(client, extra_headers={"anthropic-version": NEW})[pd.ATTR_TEMPERATURE_INFERRED] == 1.0


def test_a_removed_header_records_nothing():
    class Omit:
        pass
    assert _attrs(_client(headers={"anthropic-version": NEW}), extra_headers={"anthropic-version": Omit()}) == {}


def test_unreadable_client_headers_fall_back_to_the_override_then_the_sdk_default(monkeypatch):
    assert _attrs(_client(headers=None, custom={"anthropic-version": OLD})) == {
        pd.ATTR_API_VERSION: OLD, pd.ATTR_API_VERSION_SOURCE: "request_header"}
    monkeypatch.setattr(pd, "_sdk_default_version", lambda: NEW)
    a = _attrs(_client(headers=None))
    assert (a[pd.ATTR_API_VERSION], a[pd.ATTR_API_VERSION_SOURCE], a[pd.ATTR_TEMPERATURE_SOURCE]) == (
        NEW, "sdk_default", "provider_fixed_default")


@pytest.mark.parametrize("major, expected", [("2.0.0", NEW), ("2.6.0", NEW), ("1.17.0", OLD), ("0.96.0", OLD)])
def test_the_sdk_default_follows_the_installed_major_when_the_constant_is_missing(monkeypatch, major, expected):
    import builtins
    import importlib.metadata

    real_import = builtins.__import__

    def no_constants(name, *a, **kw):
        if name == "anthropic._constants":
            raise ImportError(name)
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_constants)
    monkeypatch.setattr(importlib.metadata, "version", lambda _: major)
    assert pd._sdk_default_version() == expected


@pytest.mark.parametrize("client", [
    _client(name="AnthropicBedrock", module="anthropic.lib.bedrock._client", headers={"anthropic-version": NEW}),
    _client(name="AnthropicVertex", module="anthropic.lib.vertex._client", headers={"anthropic-version": NEW}),
    _client(name="AnthropicFoundry", module="anthropic.lib.foundry", headers={"anthropic-version": NEW}),
    # A subclass of the first-party client (anthropic 2.x's AnthropicAWS) is another route.
    _client(name="AnthropicAWS", module="anthropic.lib.aws._client", headers={"anthropic-version": NEW}),
    # The first-party client pointed at a gateway or proxy.
    _client(headers={"anthropic-version": NEW}, base_url="https://gateway.example.com/anthropic"),
    _client(name="Anthropic", module="my_wrapper", headers={"anthropic-version": NEW}),
], ids=["bedrock", "vertex", "foundry", "aws", "gateway", "lookalike"])
def test_no_route_but_the_first_party_api_gets_anything(client):
    assert _attrs(client) == {}


# ── the SDK, end to end on the real client ─────────────────────────────────────────────────────

RESPONSE = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}


@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    enable_tool_definition_capture()
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("t"), exporter


def _call(tracing, *, default_headers=None, **kwargs):
    """One real ``messages.create`` against api.anthropic.com, served in process.
    Returns (the span's attributes, the anthropic-version the request carried)."""
    anthropic = pytest.importorskip("anthropic")
    try:  # anthropic 1.x and later are built on httpx2
        import httpx2 as hx
    except ImportError:
        import httpx as hx
    tracer, exporter = tracing
    sent = {}

    def handler(request):
        sent["version"] = request.headers.get("anthropic-version")
        return hx.Response(200, json=RESPONSE)

    client = anthropic.Anthropic(api_key="k", default_headers=default_headers,
                                 http_client=hx.Client(transport=hx.MockTransport(handler)))
    with tracer.start_as_current_span("chat", attributes={"gen_ai.operation.name": "chat"}):
        client.messages.create(model="claude-sonnet-5-5", max_tokens=10,
                               messages=[{"role": "user", "content": "hi"}], **kwargs)
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "chat"]
    return dict(span.attributes), sent["version"]


def test_the_recorded_version_is_the_header_the_installed_sdk_sent(tracing):
    attrs, sent = _call(tracing)
    assert attrs[pd.ATTR_API_VERSION] == sent
    assert attrs[pd.ATTR_API_VERSION_SOURCE] == "request_header"
    assert (pd.ATTR_TEMPERATURE_SOURCE in attrs) == (sent >= NEW)


def test_a_client_overridden_to_the_new_version_is_inferred(tracing):
    attrs, sent = _call(tracing, default_headers={"anthropic-version": NEW})
    assert sent == attrs[pd.ATTR_API_VERSION] == NEW
    assert attrs[pd.ATTR_TEMPERATURE_INFERRED] == 1.0
    assert attrs[pd.ATTR_TEMPERATURE_SOURCE] == "provider_fixed_default"


def test_a_call_overridden_to_the_old_version_infers_nothing(tracing):
    attrs, sent = _call(tracing, default_headers={"anthropic-version": NEW},
                        extra_headers={"anthropic-version": OLD}, extra_body={"temperature": 1.0})
    assert sent == attrs[pd.ATTR_API_VERSION] == OLD
    assert pd.ATTR_TEMPERATURE_SOURCE not in attrs
