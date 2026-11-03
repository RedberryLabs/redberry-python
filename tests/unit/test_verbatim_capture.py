"""A3 of the one-chain plan, part 3: the Responses API and Anthropic ``Messages.stream``, verbatim.

These record for the EVIDENCE only: one ``redberry.llm.response.raw`` event holding the request
and the response (and a stream's chunks), and no ``redberry.llm.request`` / ``redberry.llm.response``,
so what the normaliser reads, and SD1B prices, does not change. They run real clients against an
in-process transport, under the real OpenInference instrumentors where those are installed.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from redberry_sdk.instrumentors import _request_snapshot as snap
from redberry_sdk.instrumentors._stream_capture import reassemble_openai_responses
from redberry_sdk.instrumentors._tool_definitions import enable_tool_definition_capture

httpx = pytest.importorskip("httpx")


@pytest.fixture
def tracing(monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    enable_tool_definition_capture()
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _events(span, name):
    return [e for e in span.events if e.name == name]


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {k}\ndata: {json.dumps(d)}\n\n" for k, d in events).encode()


RESPONSE = {"id": "resp_1", "object": "response", "created_at": 1, "status": "completed", "model": "gpt-4o",
            "output": [{"type": "reasoning", "id": "r1", "summary": [{"type": "summary_text", "text": "Look it up."}]},
                       {"type": "message", "id": "m1", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": "INV-1002 is on hold.", "annotations": []}]}],
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 1}}}


# ── the Responses API, as the OpenAI Agents SDK calls it ──────────────────────────────────────

def test_an_agents_sdk_model_call_is_recorded_verbatim_on_its_span(tracing) -> None:
    pytest.importorskip("agents")
    instrumentation = pytest.importorskip("openinference.instrumentation.openai_agents")
    openai = pytest.importorskip("openai")
    from agents import Agent, OpenAIResponsesModel, Runner, set_tracing_disabled
    provider, exporter = tracing
    # Other tests leave the Agents SDK's tracing off or the instrumentor (a singleton) bound to
    # their provider; start from a clean state.
    set_tracing_disabled(False)
    instrumentor = instrumentation.OpenAIAgentsInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = openai.AsyncOpenAI(api_key="k", base_url="http://fake/v1", http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=RESPONSE))))
        agent = Agent(name="clerk", instructions="You are an AP clerk.",
                      model=OpenAIResponsesModel(model="gpt-4o", openai_client=client))
        out = asyncio.run(Runner.run(agent, "Is INV-1002 on hold?"))
    finally:
        instrumentor.uninstrument()
    assert out.final_output == "INV-1002 is on hold."
    (llm,) = [s for s in exporter.get_finished_spans() if s.attributes.get("openinference.span.kind") == "LLM"]
    (raw,) = _events(llm, snap.RAW_EVENT)
    assert raw.attributes["api"] == snap.API_OPENAI_RESPONSES and "stream" not in raw.attributes
    request = json.loads(raw.attributes["request"])
    assert request["instructions"] == "You are an AP clerk." and "Is INV-1002 on hold?" in json.dumps(request["input"])
    assert json.loads(raw.attributes["response"]) == RESPONSE
    # evidence only: nothing the normaliser reads was added
    assert not _events(llm, snap.REQUEST_EVENT) and not _events(llm, snap.RESPONSE_EVENT)


def _llm_span(provider):
    return provider.get_tracer("t").start_as_current_span("chat", attributes={"openinference.span.kind": "LLM"})


def test_a_streamed_responses_call_keeps_every_event_and_the_final_response(tracing) -> None:
    openai = pytest.importorskip("openai")
    provider, exporter = tracing
    events = [
        ("response.created", {"type": "response.created", "sequence_number": 0,
                              "response": {**RESPONSE, "status": "in_progress", "output": []}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "m1",
                                        "output_index": 1, "content_index": 0, "delta": "INV-1002 is on hold.",
                                        "logprobs": []}),
        ("response.completed", {"type": "response.completed", "sequence_number": 2, "response": RESPONSE}),
    ]
    client = openai.OpenAI(api_key="k", base_url="http://fake/v1", http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=_sse(events),
                                                               headers={"content-type": "text/event-stream"}))))
    with _llm_span(provider):
        got = list(client.responses.create(model="gpt-4o", input="Is INV-1002 on hold?", stream=True))
    assert [e.type for e in got] == [k for k, _ in events]
    (span,) = exporter.get_finished_spans()
    (raw,) = _events(span, snap.RAW_EVENT)
    assert raw.attributes["stream_status"] == "complete" and raw.attributes["n_chunks"] == 3
    assert json.loads(raw.attributes["response"]) == RESPONSE
    assert json.loads(raw.attributes["request"])["input"] == "Is INV-1002 on hold?"
    assert not _events(span, snap.RESPONSE_EVENT)


def test_a_responses_stream_cut_short_keeps_its_latest_snapshot() -> None:
    partial = {**RESPONSE, "status": "in_progress", "output": []}
    assert reassemble_openai_responses([{"type": "response.created", "response": partial},
                                        {"type": "response.output_text.delta", "delta": "x"}]) == partial


# ── Anthropic Messages.stream ──────────────────────────────────────────────────────────────────

THOUGHT = "Check the limit first."
ANTHROPIC = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta", "thinking": THOUGHT}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "text_delta", "text": "Holding it."}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 20}}),
    ("message_stop", {"type": "message_stop"}),
]


def _anthropic(use_async: bool):
    anthropic = pytest.importorskip("anthropic")
    try:  # anthropic 1.x is built on httpx2
        import httpx2 as hx
    except ImportError:
        hx = httpx

    def handler(request):
        return hx.Response(200, content=_sse(ANTHROPIC), headers={"content-type": "text/event-stream"})
    if use_async:
        return anthropic.AsyncAnthropic(api_key="k", base_url="http://fake",
                                        http_client=hx.AsyncClient(transport=hx.MockTransport(handler)))
    return anthropic.Anthropic(api_key="k", base_url="http://fake",
                               http_client=hx.Client(transport=hx.MockTransport(handler)))


KW = {"model": "claude-sonnet-4-5", "max_tokens": 100, "system": "You are an AP clerk.",
      "messages": [{"role": "user", "content": "Is INV-1002 on hold?"}]}


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_messages_stream_under_the_real_anthropic_instrumentor(tracing, use_async) -> None:
    instrumentation = pytest.importorskip("openinference.instrumentation.anthropic")
    provider, exporter = tracing
    instrumentor = instrumentation.AnthropicInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = _anthropic(use_async)
        if use_async:
            async def run():
                async with client.messages.stream(**KW) as stream:
                    return [e async for e in stream]
            got = asyncio.run(run())
        else:
            with client.messages.stream(**KW) as stream:
                got = list(stream)
    finally:
        instrumentor.uninstrument()
    assert "text" in {e.type for e in got}  # the caller still gets the SDK's convenience events
    (llm,) = [s for s in exporter.get_finished_spans() if s.attributes.get("openinference.span.kind") == "LLM"]
    (raw,) = _events(llm, snap.RAW_EVENT)
    assert raw.attributes["stream_status"] == "complete"
    assert [c["type"] for c in json.loads(raw.attributes["chunks"])] == [k for k, _ in ANTHROPIC]  # raw events only
    message = json.loads(raw.attributes["response"])
    assert [b["type"] for b in message["content"]] == ["thinking", "text"] and message["content"][0]["thinking"] == THOUGHT
    assert json.loads(raw.attributes["request"])["system"] == "You are an AP clerk."
    assert not _events(llm, snap.REQUEST_EVENT) and not _events(llm, snap.RESPONSE_EVENT)


def test_messages_stream_read_only_through_get_final_message_still_records_the_response(tracing) -> None:
    provider, exporter = tracing
    client = _anthropic(False)
    with _llm_span(provider):
        with client.messages.stream(**KW) as stream:
            final = stream.get_final_message()
    assert final.content[1].text == "Holding it."
    (span,) = exporter.get_finished_spans()
    (raw,) = _events(span, snap.RAW_EVENT)
    assert raw.attributes["stream_status"] == "closed"
    assert json.loads(raw.attributes["response"])["content"][1]["text"] == "Holding it."


def test_with_content_capture_off_the_request_is_not_recorded_either(tracing, monkeypatch) -> None:
    provider, exporter = tracing
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false")
    client = _anthropic(False)
    with _llm_span(provider):
        with client.messages.stream(**KW) as stream:
            list(stream)
    (span,) = exporter.get_finished_spans()
    (raw,) = _events(span, snap.RAW_EVENT)
    assert raw.attributes["omitted"] == "content_capture_disabled"
    assert "request" not in raw.attributes and "AP clerk" not in json.dumps(dict(raw.attributes))


# ── openai's raw-response wrappers ─────────────────────────────────────────────────────────────
#
# ``with_raw_response`` and ``with_streaming_response`` call the same ``create`` with a header that
# makes it return the HTTP response; the caller gets the result from ``.parse()``. The Agents SDK
# streams this way, so recording the call's return value recorded nothing and closed nothing.

STREAM_EVENTS = [
    ("response.created", {"type": "response.created", "sequence_number": 0,
                          "response": {**RESPONSE, "status": "in_progress", "output": []}}),
    ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "m1",
                                    "output_index": 1, "content_index": 0, "delta": "INV-1002 is on hold.",
                                    "logprobs": []}),
    ("response.completed", {"type": "response.completed", "sequence_number": 2, "response": RESPONSE}),
]


def _sse_transport(closed: list, *, use_async: bool):
    """A server-sent-events response whose body notes when it is closed."""
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield _sse(STREAM_EVENTS)

        def close(self):
            closed.append(True)

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse(STREAM_EVENTS)

        async def aclose(self):
            closed.append(True)

    def handler(request):
        return httpx.Response(200, stream=AsyncBody() if use_async else Body(),
                              headers={"content-type": "text/event-stream"})
    return httpx.MockTransport(handler)


def test_an_agents_sdk_streamed_run_is_recorded_verbatim_and_closes_its_response(tracing) -> None:
    import gc
    import warnings
    pytest.importorskip("agents")
    instrumentation = pytest.importorskip("openinference.instrumentation.openai_agents")
    openai = pytest.importorskip("openai")
    from agents import Agent, OpenAIResponsesModel, Runner, set_tracing_disabled
    provider, exporter = tracing
    set_tracing_disabled(False)
    instrumentor = instrumentation.OpenAIAgentsInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    closed: list = []
    try:
        client = openai.AsyncOpenAI(api_key="k", base_url="http://fake/v1",
                                    http_client=httpx.AsyncClient(transport=_sse_transport(closed, use_async=True)))
        agent = Agent(name="clerk", instructions="You are an AP clerk.",
                      model=OpenAIResponsesModel(model="gpt-4o", openai_client=client))

        async def run():
            result = Runner.run_streamed(agent, "Is INV-1002 on hold?")
            async for _ in result.stream_events():
                pass
            return result
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            out = asyncio.run(run())
            gc.collect()
    finally:
        instrumentor.uninstrument()
    assert out.final_output == "INV-1002 is on hold."
    assert not [w for w in caught if "never awaited" in str(w.message)], [str(w.message) for w in caught]
    assert closed, "the HTTP response was never closed"
    (llm,) = [s for s in exporter.get_finished_spans() if s.attributes.get("openinference.span.kind") == "LLM"]
    (raw,) = _events(llm, snap.RAW_EVENT)
    assert raw.attributes["stream_status"] == "complete" and raw.attributes["n_chunks"] == len(STREAM_EVENTS)
    assert json.loads(raw.attributes["response"]) == RESPONSE
    assert json.loads(raw.attributes["request"])["instructions"] == "You are an AP clerk."


def test_with_raw_response_records_what_parse_returns(tracing) -> None:
    openai = pytest.importorskip("openai")
    provider, exporter = tracing
    client = openai.OpenAI(api_key="k", base_url="http://fake/v1", http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=RESPONSE, headers={"x-request-id": "req_1"}))))
    with _llm_span(provider):
        raw_response = client.responses.with_raw_response.create(model="gpt-4o", input="Is INV-1002 on hold?")
        assert raw_response.headers["x-request-id"] == "req_1"        # the HTTP response, passed through
        parsed = raw_response.parse()
        assert raw_response.parse() is parsed                         # parse() twice: one record
    assert parsed.output_text == "INV-1002 is on hold."
    (span,) = exporter.get_finished_spans()
    (raw,) = _events(span, snap.RAW_EVENT)
    assert json.loads(raw.attributes["response"]) == RESPONSE and "stream" not in raw.attributes
    assert "extra_headers" not in json.loads(raw.attributes["request"])


def test_with_streaming_response_records_the_stream_parse_returns(tracing) -> None:
    openai = pytest.importorskip("openai")
    provider, exporter = tracing
    closed: list = []
    client = openai.OpenAI(api_key="k", base_url="http://fake/v1",
                           http_client=httpx.Client(transport=_sse_transport(closed, use_async=False)))
    with _llm_span(provider):
        with client.responses.with_streaming_response.create(model="gpt-4o", input="Hi", stream=True) as r:
            got = [e.type for e in r.parse()]
    assert got == [k for k, _ in STREAM_EVENTS] and closed
    (span,) = exporter.get_finished_spans()
    (raw,) = _events(span, snap.RAW_EVENT)
    assert raw.attributes["stream_status"] == "complete" and raw.attributes["n_chunks"] == len(STREAM_EVENTS)
    assert json.loads(raw.attributes["response"]) == RESPONSE


def test_chat_completions_with_raw_response_records_the_parsed_completion(tracing) -> None:
    openai = pytest.importorskip("openai")
    provider, exporter = tracing
    completion = {"id": "c1", "object": "chat.completion", "created": 1, "model": "gpt-4o",
                  "choices": [{"index": 0, "finish_reason": "stop",
                               "message": {"role": "assistant", "content": "INV-1002 is on hold."}}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    client = openai.OpenAI(api_key="k", base_url="http://fake/v1", http_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion))))
    with provider.get_tracer("t").start_as_current_span("chat", attributes={"gen_ai.operation.name": "chat"}):
        raw_response = client.chat.completions.with_raw_response.create(
            model="gpt-4o", messages=[{"role": "user", "content": "Is INV-1002 on hold?"}])
        assert raw_response.parse().choices[0].message.content == "INV-1002 is on hold."
    (span,) = exporter.get_finished_spans()
    (raw,) = _events(span, snap.RAW_EVENT)
    assert json.loads(raw.attributes["response"]) == completion
    (response,) = _events(span, snap.RESPONSE_EVENT)
    assert json.loads(response.attributes["choices"])[0]["content"] == "INV-1002 is on hold."
