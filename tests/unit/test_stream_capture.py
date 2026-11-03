"""A3 of the one-chain plan: a streamed response is recorded verbatim, and the caller loses nothing.

Before, ``create(..., stream=True)`` recorded only ``omitted = "stream"``. Now the wrapper passes the
stream through and records, when it ends, every chunk and the response they add up to
(``redberry.llm.response.raw``) and the same reasoning-and-text projection a non-streamed call gets
(``redberry.llm.response``). These run the real OpenAI and Anthropic clients against SSE served by
an in-process transport, inside a model-call span, as the native instrumentor would open it.
"""
from __future__ import annotations

import asyncio
import gc
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from redberry_sdk.instrumentors import _request_snapshot as snap
from redberry_sdk.instrumentors._stream_capture import reassemble_anthropic_messages, reassemble_openai_chat
from redberry_sdk.instrumentors._tool_definitions import enable_tool_definition_capture

httpx = pytest.importorskip("httpx")
THOUGHT = "INV-1002 is over the limit; I should hold it."


@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    enable_tool_definition_capture()
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("t"), exporter


def _llm_span(tracer):
    return tracer.start_as_current_span("chat", attributes={"gen_ai.operation.name": "chat"})


def _events(exporter, name):
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "chat"]
    return [e for e in span.events if e.name == name]


def _sse(events: list[tuple[str | None, dict | str]]) -> bytes:
    out = []
    for kind, data in events:
        body = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {kind}\n" if kind else "") + f"data: {body}\n\n")
    return "".join(out).encode()


# ── OpenAI chat completions ────────────────────────────────────────────────────────────────────

def _chunk(delta: dict, finish=None, **extra) -> dict:
    return {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "kimi-k3",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}


OPENAI_CHUNKS = [
    _chunk({"role": "assistant", "reasoning_content": "INV-1002 is over the limit; "}),
    _chunk({"reasoning_content": "I should hold it."}),
    _chunk({"content": "Holding "}),
    _chunk({"content": "it.", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                              "function": {"name": "hold_invoice", "arguments": '{"invoice'}}]}),
    _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '": "INV-1002"}'}}]}, finish="tool_calls"),
    {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "kimi-k3", "choices": [],
     "usage": {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16}},
]


def _openai(use_async: bool, body: bytes):
    openai = pytest.importorskip("openai")

    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    if use_async:
        return openai.AsyncOpenAI(api_key="k", base_url="http://fake/v1",
                                  http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return openai.OpenAI(api_key="k", base_url="http://fake/v1",
                         http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_an_openai_stream_reaches_the_caller_whole_and_is_recorded(tracing, use_async) -> None:
    tracer, exporter = tracing
    body = _sse([(None, c) for c in OPENAI_CHUNKS] + [(None, "[DONE]")])
    client = _openai(use_async, body)
    kwargs = {"model": "kimi-k3", "messages": [{"role": "user", "content": "check INV-1002"}], "stream": True}
    if use_async:
        async def run():
            with _llm_span(tracer):
                stream = await client.chat.completions.create(**kwargs)
                return [c async for c in stream]
        got = asyncio.run(run())
    else:
        with _llm_span(tracer):
            got = list(client.chat.completions.create(**kwargs))
    assert [c.model_dump(exclude_unset=True) for c in got] == OPENAI_CHUNKS  # the caller saw every chunk

    (raw,) = _events(exporter, snap.RAW_EVENT)
    assert raw.attributes["stream"] is True and raw.attributes["stream_status"] == "complete"
    assert json.loads(raw.attributes["chunks"]) == OPENAI_CHUNKS
    response = json.loads(raw.attributes["response"])
    message = response["choices"][0]["message"]
    assert message["content"] == "Holding it." and message["reasoning_content"] == THOUGHT
    assert message["tool_calls"] == [{"id": "call_1", "type": "function", "function": {
        "name": "hold_invoice", "arguments": '{"invoice": "INV-1002"}'}}]
    assert response["choices"][0]["finish_reason"] == "tool_calls" and response["usage"]["total_tokens"] == 16

    (projection,) = _events(exporter, snap.RESPONSE_EVENT)
    assert "omitted" not in projection.attributes and projection.attributes["stream"] is True
    (choice,) = json.loads(projection.attributes["choices"])
    assert (choice["reasoning"], choice["reasoning_field"], choice["content"]) == \
        (THOUGHT, "reasoning_content", "Holding it.")


def test_a_stream_dropped_part_way_is_recorded_as_abandoned(tracing) -> None:
    tracer, exporter = tracing
    client = _openai(False, _sse([(None, c) for c in OPENAI_CHUNKS] + [(None, "[DONE]")]))
    with _llm_span(tracer):
        stream = client.chat.completions.create(model="m", messages=[], stream=True)
        next(iter(stream))
        del stream
        gc.collect()
    (raw,) = _events(exporter, snap.RAW_EVENT)
    assert raw.attributes["stream_status"] == "abandoned" and raw.attributes["n_chunks"] == 1


def test_a_stream_closed_by_its_context_manager_is_recorded_as_closed(tracing) -> None:
    tracer, exporter = tracing
    client = _openai(False, _sse([(None, c) for c in OPENAI_CHUNKS] + [(None, "[DONE]")]))
    with _llm_span(tracer):
        with client.chat.completions.create(model="m", messages=[], stream=True) as stream:
            for _ in zip(range(2), stream):
                pass
    (raw,) = _events(exporter, snap.RAW_EVENT)
    assert raw.attributes["stream_status"] == "closed" and raw.attributes["n_chunks"] == 2


def test_with_content_capture_off_nothing_of_the_content_is_recorded(tracing, monkeypatch) -> None:
    tracer, exporter = tracing
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")
    client = _openai(False, _sse([(None, c) for c in OPENAI_CHUNKS] + [(None, "[DONE]")]))
    with _llm_span(tracer):
        list(client.chat.completions.create(model="m", messages=[], stream=True))
    (raw,) = _events(exporter, snap.RAW_EVENT)
    assert raw.attributes["omitted"] == "content_capture_disabled" and "chunks" not in raw.attributes
    assert THOUGHT not in json.dumps(dict(raw.attributes))
    (projection,) = _events(exporter, snap.RESPONSE_EVENT)
    assert projection.attributes["reasoning_present"] is True and "choices" not in projection.attributes


def test_over_the_limit_the_chunks_go_first_and_the_absence_is_stated(tracing, monkeypatch) -> None:
    tracer, exporter = tracing
    full = json.dumps(reassemble_openai_chat(OPENAI_CHUNKS), ensure_ascii=False)
    monkeypatch.setenv("OTELLINE_MAX_REQUEST_SNAPSHOT_BYTES", str(len(full.encode()) + 10))
    client = _openai(False, _sse([(None, c) for c in OPENAI_CHUNKS] + [(None, "[DONE]")]))
    with _llm_span(tracer):
        list(client.chat.completions.create(model="m", messages=[], stream=True))
    (raw,) = _events(exporter, snap.RAW_EVENT)
    assert raw.attributes["omitted"] == "chunks_size" and "chunks" not in raw.attributes
    assert json.loads(raw.attributes["response"])["choices"][0]["message"]["content"] == "Holding it."


def test_a_non_streamed_response_is_recorded_verbatim_too(tracing) -> None:
    openai = pytest.importorskip("openai")
    tracer, exporter = tracing
    body = {"id": "c2", "object": "chat.completion", "created": 1, "model": "kimi-k3", "system_fingerprint": "fp",
            "choices": [{"index": 0, "finish_reason": "stop", "logprobs": None,
                         "message": {"role": "assistant", "content": "Done.", "reasoning_content": THOUGHT}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    client = openai.OpenAI(api_key="k", base_url="http://fake/v1", http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    with _llm_span(tracer):
        client.chat.completions.create(model="kimi-k3", messages=[])
    (raw,) = _events(exporter, snap.RAW_EVENT)
    assert json.loads(raw.attributes["response"]) == body and "stream" not in raw.attributes


# ── Anthropic messages ─────────────────────────────────────────────────────────────────────────

ANTHROPIC_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta", "thinking": THOUGHT}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "signature_delta", "signature": "sig"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": "Holding it."}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ("content_block_start", {"type": "content_block_start", "index": 2, "content_block": {
        "type": "tool_use", "id": "toolu_1", "name": "hold_invoice", "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": '{"invoice": '}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 2,
                             "delta": {"type": "input_json_delta", "partial_json": '"INV-1002"}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 2}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                       "usage": {"output_tokens": 40}}),
    ("message_stop", {"type": "message_stop"}),
]


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_an_anthropic_stream_is_recorded(tracing, use_async) -> None:
    anthropic = pytest.importorskip("anthropic")
    tracer, exporter = tracing
    body = _sse(ANTHROPIC_EVENTS)
    try:  # anthropic 1.x is built on httpx2, earlier releases on httpx
        import httpx2 as hx
    except ImportError:
        hx = httpx

    def handler(request):
        return hx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    kwargs = {"model": "claude-sonnet-4-5", "max_tokens": 100, "stream": True,
              "messages": [{"role": "user", "content": "check INV-1002"}]}
    if use_async:
        client = anthropic.AsyncAnthropic(api_key="k", base_url="http://fake",
                                          http_client=hx.AsyncClient(transport=hx.MockTransport(handler)))

        async def run():
            with _llm_span(tracer):
                stream = await client.messages.create(**kwargs)
                return [e async for e in stream]
        got = asyncio.run(run())
    else:
        client = anthropic.Anthropic(api_key="k", base_url="http://fake",
                                     http_client=hx.Client(transport=hx.MockTransport(handler)))
        with _llm_span(tracer):
            got = list(client.messages.create(**kwargs))
    assert [e.type for e in got] == [k for k, _ in ANTHROPIC_EVENTS]

    (raw,) = _events(exporter, snap.RAW_EVENT)
    message = json.loads(raw.attributes["response"])
    assert [b["type"] for b in message["content"]] == ["thinking", "text", "tool_use"]
    assert message["content"][0]["thinking"] == THOUGHT and message["content"][0]["signature"] == "sig"
    assert message["content"][2]["input"] == {"invoice": "INV-1002"}
    assert message["stop_reason"] == "tool_use" and message["usage"]["output_tokens"] == 40
    (projection,) = _events(exporter, snap.RESPONSE_EVENT)
    (choice,) = json.loads(projection.attributes["choices"])
    assert (choice["reasoning"], choice["content"]) == (THOUGHT, "Holding it.")


def test_reassembly_keeps_tool_input_that_does_not_parse() -> None:
    msg = reassemble_anthropic_messages([
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "n"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a": '}},
    ])
    assert msg["content"][0]["input_json"] == '{"a": '  # cut off mid-stream: kept as streamed


def test_patching_twice_does_not_record_twice(tracing) -> None:
    tracer, exporter = tracing
    enable_tool_definition_capture()  # a second init(): nothing is wrapped again
    client = _openai(False, _sse([(None, c) for c in OPENAI_CHUNKS] + [(None, "[DONE]")]))
    with _llm_span(tracer):
        list(client.chat.completions.create(model="m", messages=[], stream=True))
    assert len(_events(exporter, snap.RAW_EVENT)) == 1 and len(_events(exporter, snap.RESPONSE_EVENT)) == 1
