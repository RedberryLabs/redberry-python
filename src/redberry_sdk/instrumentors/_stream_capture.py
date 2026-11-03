"""Record a STREAMED response, verbatim, without taking it from the caller.

``create(..., stream=True)`` returns an iterator the caller consumes. Reading it
here would consume it, so the SDK used to record nothing (``omitted =
"stream"``). Now the wrapper returns a pass-through in its place: each chunk goes
to the caller unchanged and is kept, and when the stream ends — exhausted, failed,
closed, or dropped unread — the SDK records, on the model call's span:

- ``redberry.llm.response.raw``: every chunk in order, and the response
  reassembled from them, with how the stream ended (``stream_status``);
- ``redberry.llm.response``: the same reasoning-and-text projection a
  non-streamed response gets, built from the reassembled response, so the
  normaliser reads a streamed call exactly as a non-streamed one.

The span is the one current when the call was made (the native instrumentor's
model-call span), held by the wrapper: the caller iterates later, outside it.
The instrumentor ends that span only once the stream is done, after this
records. A stream dropped unread is recorded when it is collected, if the span
is still open; otherwise the loss is logged.

Covered: OpenAI chat completions ``stream=True`` and Anthropic
``Messages.create(stream=True)``, sync and async, as above; and, recorded
verbatim only (``redberry.llm.response.raw`` with the request beside it, no
projection), the Responses API (``Responses.create``, the OpenAI Agents SDK's
model call) streamed or not, which the normaliser reads from that record, and
Anthropic ``Messages.stream``, evidence only. (RAP docs/valp_single_chain_plan.md,
A3.)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from redberry_sdk.instrumentors._request_snapshot import (
    API_ANTHROPIC_MESSAGES,
    API_OPENAI_RESPONSES,
    RAW_EVENT,
    RESPONSE_EVENT,
    _as_dict,
    build_raw_attributes,
    build_response_attributes,
)

logger = logging.getLogger(__name__)

__all__ = ["capture_stream", "capture_message_stream", "reassemble_openai_chat", "reassemble_anthropic_messages",
           "reassemble_openai_responses"]

#: Anthropic's own protocol events: what a ``Messages.stream`` chunk list keeps (the
#: SDK's MessageStream also yields convenience events, ``text`` and the like).
ANTHROPIC_RAW_EVENTS = frozenset({"message_start", "content_block_start", "content_block_delta",
                                  "content_block_stop", "message_delta", "message_stop"})


# ----------------------------------------------------------------------
# Reassembly: the response the chunks add up to
# ----------------------------------------------------------------------

def reassemble_openai_chat(chunks: list[dict]) -> dict:
    """A ``chat.completion`` from ``chat.completion.chunk`` dicts: text and
    reasoning concatenated, tool calls assembled by index, the last
    ``finish_reason``, and ``usage`` if a chunk carried it."""
    out: dict[str, Any] = {"object": "chat.completion", "choices": []}
    choices: dict[int, dict] = {}
    for chunk in chunks:
        for key in ("id", "created", "model", "system_fingerprint", "service_tier"):
            if chunk.get(key) is not None:
                out[key] = chunk[key]
        if chunk.get("usage") is not None:
            out["usage"] = chunk["usage"]
        for c in chunk.get("choices") or []:
            index = c.get("index", 0)
            choice = choices.setdefault(index, {"index": index, "message": {"role": "assistant"},
                                                "finish_reason": None, "_calls": {}})
            delta = c.get("delta") or {}
            msg = choice["message"]
            if delta.get("role"):
                msg["role"] = delta["role"]
            for field in ("content", "reasoning_content", "reasoning", "refusal"):
                if isinstance(delta.get(field), str):
                    msg[field] = msg.get(field, "") + delta[field]
            for call in delta.get("tool_calls") or []:
                slot = choice["_calls"].setdefault(call.get("index", 0),
                                                   {"type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                if call.get("type"):
                    slot["type"] = call["type"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if c.get("finish_reason") is not None:
                choice["finish_reason"] = c["finish_reason"]
    for index in sorted(choices):
        choice = choices[index]
        calls = choice.pop("_calls")
        if calls:
            choice["message"]["tool_calls"] = [calls[i] for i in sorted(calls)]
        choice["message"].setdefault("content", None)
        out["choices"].append(choice)
    return out


def reassemble_anthropic_messages(events: list[dict]) -> dict:
    """A ``message`` from Anthropic stream events: ``message_start`` gives the
    message, each ``content_block_*`` builds a block (text, thinking and its
    signature, tool_use and its JSON input), ``message_delta`` the stop reason
    and output usage."""
    message: dict[str, Any] = {"type": "message", "role": "assistant", "content": []}
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    for ev in events:
        kind = ev.get("type")
        if kind == "message_start":
            message.update({k: v for k, v in (ev.get("message") or {}).items() if k != "content"})
        elif kind == "content_block_start":
            blocks[ev.get("index", 0)] = dict(ev.get("content_block") or {})
        elif kind == "content_block_delta":
            index, delta = ev.get("index", 0), ev.get("delta") or {}
            block = blocks.setdefault(index, {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif dtype == "input_json_delta":
                partial[index] = partial.get(index, "") + delta.get("partial_json", "")
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))
        elif kind == "message_delta":
            delta = ev.get("delta") or {}
            for key in ("stop_reason", "stop_sequence"):
                if key in delta:
                    message[key] = delta[key]
            if ev.get("usage"):
                message["usage"] = {**(message.get("usage") or {}), **ev["usage"]}
    for index, text in partial.items():
        block = blocks.setdefault(index, {})
        try:
            block["input"] = json.loads(text) if text else {}
        except ValueError:
            block["input_json"] = text  # as streamed, when it does not parse
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message


def reassemble_openai_responses(events: list[dict]) -> dict:
    """The Responses API sends the whole response in its last lifecycle event
    (``response.completed``; ``failed`` or ``incomplete`` when it ends otherwise);
    a stream cut short keeps the latest snapshot it carried."""
    final = None
    for ev in events:
        if isinstance(ev.get("response"), dict):
            final = ev["response"]
    return final if final is not None else {"object": "response", "output": []}


def _reassemble(api: str, chunks: list[dict]) -> dict:
    if api == API_ANTHROPIC_MESSAGES:
        return reassemble_anthropic_messages(chunks)
    if api == API_OPENAI_RESPONSES:
        return reassemble_openai_responses(chunks)
    return reassemble_openai_chat(chunks)


# ----------------------------------------------------------------------
# The pass-through
# ----------------------------------------------------------------------

class _Capture:
    """The chunks seen so far, and recording them once, however the stream ends."""

    def __init__(self, span: Any, api: str, *, raw_only: bool = False, request: dict | None = None,
                 keep: Any = None, final: Any = None) -> None:
        self.span, self.api = span, api
        self.raw_only, self.request = raw_only, request   # evidence only: no projection, request beside
        self.keep, self.final = keep, final               # which chunks to keep; the SDK's own final response
        self.chunks: list[dict] = []
        self.done = False

    def add(self, chunk: Any) -> None:
        item = _as_dict(chunk)
        if self.keep is None or self.keep(item):
            self.chunks.append(item)

    def finish(self, status: str) -> None:
        if self.done:
            return
        self.done = True
        try:
            if not self.span.is_recording():
                logger.warning("a streamed LLM response ended (%s) after its span: not recorded", status)
                return
            snapshot = self.final() if self.final is not None else None
            response = _as_dict(snapshot) if snapshot is not None else _reassemble(self.api, self.chunks)
            if not self.raw_only:
                projection = build_response_attributes(response, self.api)
                projection.update(stream=True, stream_status=status)
                self.span.add_event(RESPONSE_EVENT, attributes=projection)
            self.span.add_event(RAW_EVENT, attributes=build_raw_attributes(
                self.api, response=response, chunks=self.chunks, status=status, request=self.request))
        except Exception as e:                               # noqa: BLE001 - a capture gap, never the caller's error
            logger.warning("failed to record a streamed response: %s", e)


class CapturedStream:
    """A sync stream, passed through. Attributes the caller uses on the stream
    itself (``response``, ``close``) reach the original."""

    def __init__(self, inner: Any, capture: _Capture) -> None:
        self._inner, self._capture, self._it = inner, capture, None

    def __iter__(self) -> CapturedStream:
        return self

    def __next__(self) -> Any:
        if self._it is None:
            self._it = iter(self._inner)
        try:
            item = next(self._it)
        except StopIteration:
            self._capture.finish("complete")
            raise
        except BaseException as e:
            self._capture.finish(f"error:{type(e).__name__}")
            raise
        self._capture.add(item)
        return item

    def close(self) -> None:
        self._capture.finish("closed")
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> CapturedStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        # Our own fields, if unset, must not recurse; every other name (instrumentors reach into
        # private ones, OpenInference reads ``_raw_stream``) is the wrapped stream's.
        if name in ("_inner", "_capture", "_it"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __del__(self) -> None:
        try:
            self._capture.finish("abandoned")
        except Exception:                                    # noqa: BLE001
            pass


class AsyncCapturedStream:
    """An async stream, passed through."""

    def __init__(self, inner: Any, capture: _Capture) -> None:
        self._inner, self._capture, self._it = inner, capture, None

    def __aiter__(self) -> AsyncCapturedStream:
        return self

    async def __anext__(self) -> Any:
        if self._it is None:
            self._it = self._inner.__aiter__()
        try:
            item = await self._it.__anext__()
        except StopAsyncIteration:
            self._capture.finish("complete")
            raise
        except BaseException as e:
            self._capture.finish(f"error:{type(e).__name__}")
            raise
        self._capture.add(item)
        return item

    async def close(self) -> None:
        self._capture.finish("closed")
        close = getattr(self._inner, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def __aenter__(self) -> AsyncCapturedStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        # Our own fields, if unset, must not recurse; every other name (instrumentors reach into
        # private ones, OpenInference reads ``_raw_stream``) is the wrapped stream's.
        if name in ("_inner", "_capture", "_it"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __del__(self) -> None:
        try:
            self._capture.finish("abandoned")
        except Exception:                                    # noqa: BLE001
            pass


def capture_stream(stream: Any, span: Any, api: str, *, raw_only: bool = False,
                   request: dict | None = None) -> Any:
    """``stream`` passed through, recording onto ``span`` as it is consumed."""
    capture = _Capture(span, api, raw_only=raw_only, request=request)
    if hasattr(stream, "__aiter__") and not hasattr(stream, "__iter__"):
        return AsyncCapturedStream(stream, capture)
    return CapturedStream(stream, capture)


class _CapturedManager:
    """Anthropic's ``Messages.stream(...)`` returns a context manager whose
    ``__enter__`` gives the MessageStream; this hands back that stream passed
    through. The response recorded is the SDK's own accumulated message
    (``current_message_snapshot``), so a caller that never iterates (only
    ``get_final_message()``) still leaves the response, if not its chunks."""

    def __init__(self, inner: Any, span: Any, request: dict) -> None:
        self._inner, self._span, self._request, self._capture = inner, span, request, None

    def _wrap(self, stream: Any) -> Any:
        self._capture = _Capture(self._span, API_ANTHROPIC_MESSAGES, raw_only=True, request=self._request,
                                 keep=lambda e: e.get("type") in ANTHROPIC_RAW_EVENTS,
                                 final=lambda: getattr(stream, "current_message_snapshot", None))
        if hasattr(stream, "__aiter__") and not hasattr(stream, "__iter__"):
            return AsyncCapturedStream(stream, self._capture)
        return CapturedStream(stream, self._capture)

    def __enter__(self) -> Any:
        return self._wrap(self._inner.__enter__())

    def __exit__(self, *exc: Any) -> Any:
        if self._capture is not None:
            self._capture.finish(f"error:{exc[0].__name__}" if exc and exc[0] else "closed")
        return self._inner.__exit__(*exc)

    async def __aenter__(self) -> Any:
        return self._wrap(await self._inner.__aenter__())

    async def __aexit__(self, *exc: Any) -> Any:
        if self._capture is not None:
            self._capture.finish(f"error:{exc[0].__name__}" if exc and exc[0] else "closed")
        return await self._inner.__aexit__(*exc)

    def __getattr__(self, name: str) -> Any:
        if name in ("_inner", "_span", "_request", "_capture"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def capture_message_stream(manager: Any, span: Any, request: dict) -> Any:
    """``Messages.stream(...)``'s manager, passed through and recorded (evidence only)."""
    return _CapturedManager(manager, span, request)
