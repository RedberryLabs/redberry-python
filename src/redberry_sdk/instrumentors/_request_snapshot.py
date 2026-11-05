"""Record the request an LLM call was ACTUALLY made with, verbatim.

The upstream instrumentors record a model's input as one event per message, and
each rebuilds the message from the parts it recognises. That rebuild loses
things the model saw, silently:

- ``opentelemetry-instrumentation-openai-v2`` (legacy events, the path every
  OpenAI-backed agent here runs on) *replaces* an assistant message's body with
  its ``tool_calls`` — so any text the assistant said alongside a tool call is
  gone — and strips every newline from the tool-call arguments;
- none of them record the decoding parameters beyond a fixed handful, so
  ``logprobs``, ``n``, ``parallel_tool_calls``, ``response_format`` and anything
  passed through ``extra_body`` (``top_k`` on most OpenAI-compatible hosts) never
  reach the trace;
- the OTel SDK keeps at most ``OTEL_SPAN_EVENT_COUNT_LIMIT`` (128) events per
  span and evicts the OLDEST first, so a long conversation loses its opening
  messages — the system prompt and the user's request — without a trace.

CTA conditions on this context. Every one of those losses moves the posterior,
and none of them is visible downstream. So the SDK records the request itself:
one span event, ``redberry.llm.request``, holding the ``messages`` exactly as
passed to the client, plus every other request parameter. The normaliser reads
it in preference to the per-message events.

What is never recorded
----------------------
- **Content, when content capture is off.** ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT``
  is the switch every instrumentor honours, and ``redberry_sdk.init`` sets it
  from ``capture_content``. Off, the event carries the decoding parameters only
  and says why the messages are absent.
- **Transport options** — ``extra_headers`` (API keys travel there),
  ``extra_query``, ``timeout``.

It is an EVENT, not an attribute. VALP stores full spans, events included, in
the insured's own storage, so this snapshot is part of the evidence there; it
leaves the insured only in a disclosure the insured approves (VALP sends Redberry
headers, metrics, statements and receipts, never content).

Size
----
Nothing is ever clipped. A request larger than the limit is recorded WITHOUT its
messages and with ``omitted = "size"`` and its byte count, so the normaliser can
say "this context was too large to record" rather than present a shortened one
as complete. The limit is ``REDBERRY_MAX_REQUEST_SNAPSHOT_BYTES`` (default 4 MiB).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from opentelemetry import trace
from .. import _env

logger = logging.getLogger(__name__)

#: The event the normaliser reads (`redberry_normaliser._request_snapshot`).
REQUEST_EVENT = "redberry.llm.request"

#: Bumped when the event's attribute layout changes.
SNAPSHOT_VERSION = 1

DEFAULT_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
_MAX_BYTES_ENV = "REDBERRY_MAX_REQUEST_SNAPSHOT_BYTES"
_CONTENT_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

#: Request shapes the normaliser knows how to read back.
API_OPENAI_CHAT = "openai.chat_completions"
API_ANTHROPIC_MESSAGES = "anthropic.messages"
#: The Responses API (the OpenAI Agents SDK's default model). Recorded verbatim only
#: (``redberry.llm.response.raw``, the request beside the response); the normaliser reads
#: the call from that record (``redberry_normaliser._raw_record``).
API_OPENAI_RESPONSES = "openai.responses"

#: Keys that are the conversation, not parameters. Recorded separately.
_CONTENT_KEYS = frozenset({"messages", "system"})
#: Recorded elsewhere (`gen_ai.request.tools`, see `_tool_definitions`).
_TOOL_KEYS = frozenset({"tools", "tool_choice"})
#: Transport options: never recorded, whatever the content setting.
_TRANSPORT_KEYS = frozenset({"extra_headers", "extra_query", "timeout"})
#: Parameters that can carry content themselves; dropped when content capture is off.
_CONTENT_BEARING_PARAMS = frozenset({
    "extra_body", "metadata", "user", "response_format", "prediction",
    "logit_bias", "store", "audio",
})


def max_snapshot_bytes() -> int:
    raw = _env.get(_MAX_BYTES_ENV)
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("%s=%r is not a positive integer; using %d",
                           _MAX_BYTES_ENV, raw, DEFAULT_MAX_SNAPSHOT_BYTES)
    return DEFAULT_MAX_SNAPSHOT_BYTES


def content_capture_enabled() -> bool:
    return os.environ.get(_CONTENT_ENV, "false").strip().lower() == "true"


def _jsonable(obj: Any) -> Any:
    """`json.dumps` default: SDK models and `NotGiven`-style sentinels."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_unset=True)
        except TypeError:
            return dump()
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return {"_bytes": len(obj)}
    return str(obj)


def _dumps(value: Any) -> str:
    # Order preserved (no sort_keys) and non-ASCII kept as-is: this is a record
    # of what was sent, and both are part of what the model saw.
    return json.dumps(value, default=_jsonable, ensure_ascii=False)


def _is_given(value: Any) -> bool:
    # openai.NOT_GIVEN / anthropic.NOT_GIVEN / openai.Omit: "the caller did not
    # pass this". Recording them as a value would claim a parameter was set.
    return type(value).__name__ not in ("NotGiven", "Omit", "NoneType") and value is not None


def build_request_attributes(kwargs: dict, api: str) -> dict[str, Any]:
    """The event attributes for one request. Pure; exposed for tests."""
    capture = content_capture_enabled()
    msgs = kwargs.get("messages")
    if msgs is not None and not isinstance(msgs, (list, tuple)):
        # A one-shot iterable: serialising it would hand the client an exhausted
        # one. Materialised in place, so the call receives the same messages.
        msgs = kwargs["messages"] = list(msgs)
    params = {
        k: v for k, v in kwargs.items()
        if k not in _CONTENT_KEYS | _TOOL_KEYS | _TRANSPORT_KEYS and _is_given(v)
        and (capture or k not in _CONTENT_BEARING_PARAMS)
    }
    attributes: dict[str, Any] = {
        "api": api,
        "snapshot_version": SNAPSHOT_VERSION,
        "params": _dumps(params),
        "message_count": len(msgs or []),
    }
    if not capture:
        attributes["omitted"] = "content_capture_disabled"
        return attributes

    messages = _dumps(msgs or [])
    system = kwargs.get("system")
    system_json = _dumps(system) if _is_given(system) else None
    size = len(messages.encode()) + (len(system_json.encode()) if system_json else 0)
    attributes["content_bytes"] = size
    limit = max_snapshot_bytes()
    if size > limit:
        logger.warning(
            "LLM request is %d bytes, over the %d-byte snapshot limit (%s); "
            "recording its parameters and marking the messages omitted rather "
            "than recording a shortened conversation as if it were complete",
            size, limit, _MAX_BYTES_ENV)
        attributes["omitted"] = "size"
        attributes["limit_bytes"] = limit
        return attributes
    attributes["messages"] = messages
    if system_json is not None:
        attributes["system"] = system_json
    return attributes


def record_request_on_current_span(kwargs: dict, api: str,
                                   server_address: str | None = None) -> bool:
    """Attach this request's verbatim snapshot to the LLM span in progress.

    ``server_address`` is the host the client calls (its ``base_url``); it is
    recorded beside the snapshot so the normaliser can name the provider that
    served the call when the instrumentor sets no ``server.address``.

    Returns whether a snapshot was recorded; False means there was no recording
    span to put it on (the instrumentor is absent, or tracing is suppressed).
    """
    span = trace.get_current_span()
    if span is None or not span.is_recording() or not _is_llm_span(span):
        return False
    attributes = build_request_attributes(kwargs, api)
    if server_address:
        attributes["server_address"] = server_address
    span.add_event(REQUEST_EVENT, attributes=attributes)
    return True


#: Attributes by which each dialect marks a model-call span at span start.
_LLM_SPAN_MARKERS = ("gen_ai.operation.name", "gen_ai.system", "llm.request.type")


def _is_llm_span(span: Any) -> bool:
    """Whether the current span is the model call's own span.

    With the native instrumentor absent or suppressed (an application may suppress
    some of its own calls), the current span is some enclosing application span, and
    a conversation recorded there would be attributed to the wrong call — or, for
    a suppressed call, recorded at all. A span whose attributes cannot be read is
    given the benefit of the doubt: the normaliser only reads this event from
    spans it has itself classified as model calls.
    """
    try:
        from opentelemetry.instrumentation.utils import is_instrumentation_enabled
        if not is_instrumentation_enabled():
            return False
    except ImportError:
        pass
    attrs = getattr(span, "attributes", None)
    if attrs is None:
        return True
    return (any(k in attrs for k in _LLM_SPAN_MARKERS)
            or attrs.get("openinference.span.kind") == "LLM")


# ----------------------------------------------------------------------
# The response's REASONING
# ----------------------------------------------------------------------
#
# No instrumentor records what a reasoning model thought before it answered.
# `opentelemetry-instrumentation-openai-v2` builds its `gen_ai.choice` event
# from `content` and `tool_calls` only, so the `reasoning_content` an
# OpenAI-compatible host returns beside them (DeepSeek, Kimi, and every such
# model on Fireworks) never reaches the span. That reasoning is the prefix the
# model's tool call was conditioned on: CTA re-renders the conversation and
# scores the call, and without it the surrogate reaches the tool-name token
# with none of the thinking that chose the tool.
#
# So the same wrapper records it, as a second event, `redberry.llm.response`,
# on the same span: per choice, the reasoning text and the field it came in.
# Written for every non-streamed response, reasoning or not, so the normaliser
# can tell "the model returned none" from "nothing looked".
#
# Beside the reasoning it records the response's TEXT — the `content` an
# OpenAI-compatible message carries alongside its `tool_calls` ("INV-1002 is
# over limit, so I'll hold it"), or an Anthropic response's text blocks before
# its first `tool_use`. That text is the rest of the prefix the call was
# conditioned on. The instrumentor may keep a copy, but nothing says whether
# it looked: an absent `content` there is "unknown", here it is "none" (`""`).

#: The event the normaliser reads (`redberry_normaliser._request_snapshot`).
RESPONSE_EVENT = "redberry.llm.response"

#: OpenAI-compatible message fields that carry reasoning, in preference order.
#: `reasoning_content`: DeepSeek, Kimi/Moonshot, Fireworks, vLLM. `reasoning`:
#: OpenRouter, some vLLM builds.
REASONING_FIELDS = ("reasoning_content", "reasoning")


def _as_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    dumped = _jsonable(obj)
    return dumped if isinstance(dumped, dict) else {}


def _get(obj: Any, name: str) -> Any:
    """An attribute of an SDK object, or a key of the dict a stream was reassembled into."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _reasoning_tokens(usage: Any) -> int | None:
    """`usage.completion_tokens_details.reasoning_tokens` (OpenAI and most
    compatible hosts; some put it under `output_tokens_details`)."""
    u = _as_dict(usage) if usage is not None else {}
    for key in ("completion_tokens_details", "output_tokens_details"):
        n = (u.get(key) or {}).get("reasoning_tokens") if isinstance(u.get(key), dict) else None
        if isinstance(n, int):
            return n
    return None


def _openai_content(content: Any) -> str:
    """An OpenAI-compatible message's text: a string as is, a part list's text
    parts joined. None (a tool-call message with no text) is ``""``."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or p.get("refusal") or "") for p in content
                       if isinstance(p, dict)
                       and p.get("type") in ("text", "output_text", "refusal"))
    return ""


def _openai_choices(response: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k, choice in enumerate(_get(response, "choices") or []):
        msg = _as_dict(_get(choice, "message") or {})
        index = _get(choice, "index")
        entry: dict[str, Any] = {"index": k if index is None else index, "reasoning": None,
                                 "reasoning_field": None,
                                 "content": _openai_content(msg.get("content"))}
        for field in REASONING_FIELDS:
            text = msg.get(field)
            if isinstance(text, str) and text:
                entry["reasoning"], entry["reasoning_field"] = text, field
                break
        out.append(entry)
    return out


def _anthropic_choices(response: Any) -> list[dict[str, Any]]:
    texts: list[str] = []
    content: list[str] = []
    redacted = 0
    seen_call = False
    for block in _get(response, "content") or []:
        b = _as_dict(block)
        if b.get("type") == "thinking" and isinstance(b.get("thinking"), str):
            texts.append(b["thinking"])
        elif b.get("type") == "redacted_thinking":
            redacted += 1
        elif b.get("type") == "tool_use":
            seen_call = True
        elif b.get("type") == "text" and not seen_call and isinstance(b.get("text"), str):
            # Only text BEFORE the first call is a prefix the call was
            # conditioned on; text after it came later.
            content.append(b["text"])
    return [{"index": 0, "reasoning": "".join(texts) or None,
             "reasoning_field": "thinking" if texts else None, "redacted": redacted,
             "content": "".join(content)}]


def build_response_attributes(response: Any, api: str) -> dict[str, Any]:
    """The event attributes for one non-streamed response. Pure; exposed for tests."""
    attributes: dict[str, Any] = {"api": api, "snapshot_version": SNAPSHOT_VERSION}
    if api == API_ANTHROPIC_MESSAGES:
        choices = _anthropic_choices(response)
        # Anthropic bills thinking as output and does not break it out.
        n_tokens = None
    else:
        choices = _openai_choices(response)
        n_tokens = _reasoning_tokens(_get(response, "usage"))
    if n_tokens is not None:
        attributes["reasoning_tokens"] = n_tokens
    attributes["n_choices"] = len(choices)
    if not content_capture_enabled():
        # Whether there was any is not content; what it said is.
        attributes["reasoning_present"] = any(c["reasoning"] for c in choices)
        attributes["content_present"] = any(c["content"] for c in choices)
        attributes["omitted"] = "content_capture_disabled"
        return attributes
    payload = _dumps(choices)
    limit = max_snapshot_bytes()
    if len(payload.encode()) > limit:
        logger.warning(
            "LLM response reasoning and text are %d bytes, over the %d-byte snapshot limit "
            "(%s); recording that it was omitted rather than a shortened copy",
            len(payload.encode()), limit, _MAX_BYTES_ENV)
        attributes["reasoning_present"] = any(c["reasoning"] for c in choices)
        attributes["content_present"] = any(c["content"] for c in choices)
        attributes["omitted"] = "size"
        attributes["content_bytes"] = len(payload.encode())
        attributes["limit_bytes"] = limit
        return attributes
    attributes["choices"] = payload
    return attributes


# ----------------------------------------------------------------------
# The response, VERBATIM
# ----------------------------------------------------------------------
#
# The projection above is what the normaliser reads. The evidence keeps
# everything the model returned: `redberry.llm.response.raw` holds the response
# as the client parsed it (the fields set on the wire, `exclude_unset`), and for
# a stream every chunk in order plus the response reassembled from them
# (`_stream_capture`). VALP stores it in the insured's own buckets; it leaves
# the insured only in a disclosure they approve.

#: The verbatim response, beside the projection.
RAW_EVENT = "redberry.llm.response.raw"


def request_record(kwargs: dict) -> dict[str, Any]:
    """The request as the caller passed it, for the verbatim record: every given
    parameter except transport options (``extra_headers`` carries API keys)."""
    return {k: v for k, v in kwargs.items() if k not in _TRANSPORT_KEYS and _is_given(v)}


def build_raw_attributes(api: str, *, response: Any, chunks: list | None = None,
                         status: str | None = None, request: dict | None = None) -> dict[str, Any]:
    """The event attributes for one response, verbatim. Pure; exposed for tests.

    ``request`` is recorded beside it for the calls no ``redberry.llm.request``
    covers (the Responses API, ``Messages.stream``). Over the snapshot limit, a
    stream's chunks go first, then the request (the response is kept if it fits),
    then the response; what is left out is stated, never shortened."""
    attributes: dict[str, Any] = {"api": api, "snapshot_version": SNAPSHOT_VERSION}
    if chunks is not None:
        attributes.update(stream=True, stream_status=status or "complete", n_chunks=len(chunks))
    if not content_capture_enabled():
        attributes["omitted"] = "content_capture_disabled"
        return attributes
    parts = {"response": _dumps(response)}
    if request is not None:
        parts["request"] = _dumps(request)
    if chunks is not None:
        parts["chunks"] = _dumps(chunks)
    size = sum(len(v.encode()) for v in parts.values())
    limit = max_snapshot_bytes()
    if size > limit:
        logger.warning("LLM call is %d bytes verbatim, over the %d-byte snapshot limit (%s); "
                       "recording what was left out rather than a shortened copy", size, limit, _MAX_BYTES_ENV)
        attributes.update(content_bytes=size, limit_bytes=limit)
        dropped = []
        for name in ("chunks", "request"):
            if name in parts and sum(len(v.encode()) for v in parts.values()) > limit:
                del parts[name]
                dropped.append(name)
        if sum(len(v.encode()) for v in parts.values()) > limit:
            attributes["omitted"] = "size"
            return attributes
        attributes["omitted"] = "chunks_size" if dropped == ["chunks"] else "_".join(dropped) + "_size"
    attributes.update(parts)
    return attributes


def record_response_on_current_span(response: Any, kwargs: dict, api: str) -> bool:
    """Attach this response to the LLM span in progress: its reasoning and text
    (``redberry.llm.response``) and the response itself (``redberry.llm.response.raw``).

    A streamed response is recorded by the stream's own wrapper
    (``_stream_capture``) as it is consumed. Reaching here with one means that
    wrapper could not be put in place; it is recorded as ``omitted = "stream"``
    so the absence is stated.
    """
    span = trace.get_current_span()
    if span is None or not span.is_recording() or not _is_llm_span(span):
        return False
    if kwargs.get("stream") is True:
        span.add_event(RESPONSE_EVENT, attributes={
            "api": api, "snapshot_version": SNAPSHOT_VERSION, "omitted": "stream"})
        return True
    span.add_event(RESPONSE_EVENT, attributes=build_response_attributes(response, api))
    span.add_event(RAW_EVENT, attributes=build_raw_attributes(api, response=response))
    return True
