"""Record the tool DEFINITIONS a request was made with.

The upstream instrumentors record which tool the model *called*. None of them
record which tools it could have called. `opentelemetry-instrumentation-openai-v2`
— the instrumentor every OpenAI-backed agent here runs on — contains no
reference to `tools` at all, so the request's tool list never reaches a span.

That absence propagates the whole way down, silently:

    instrumentor emits no tool definitions
      -> the adapter's available_tools_json is None
      -> _mapper._find_tool_schema returns None
      -> C0's ToolCallEvent.tool_schema is None
      -> SD1B sends json.dumps(None or {}) == "{}"
      -> CTA scores the event against no schema, and any control that
         validates arguments against one has nothing to validate against

Measured consequence: all 56 captured `/v1/score-event` payloads carry
`tool_schema: "{}"`, and the same agent's measured survival moves from 0.2857
to 0.6429 depending only on whether a schema is present — a difference about
the corpus, not the agent.

**The reader already exists.** `dialect_adapters/_otel_genai_v2` parses a
`gen_ai.request.tools` span event and `_openinference` parses
`llm.tools.{i}.tool.*` attributes. Only the writer was missing. This supplies
it, in the event shape the genai_v2 adapter already reads, so nothing
downstream changes.

Ordering
--------
This must be applied BEFORE the native instrumentors. wrapt wrappers nest in
application order: the last patch applied is the outermost. Patching first
makes this the INNER wrapper, which is what puts it inside the LLM span the
official instrumentor opens — so `get_current_span()` is that span and the
event lands on it. Applied afterwards it would run outside, and the event would
attach to whatever unrelated span happened to be current, or to none.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
from collections.abc import Mapping
from typing import Any, Callable

from opentelemetry import trace

from ._request_snapshot import (
    API_ANTHROPIC_MESSAGES, API_OPENAI_CHAT, API_OPENAI_RESPONSES, RAW_EVENT, _is_llm_span, build_raw_attributes,
    record_request_on_current_span, record_response_on_current_span, request_record,
)
from ._stream_capture import capture_message_stream, capture_stream

logger = logging.getLogger(__name__)

#: The event the genai_v2 adapter already looks for.
TOOLS_EVENT = "gen_ai.request.tools"

#: Span attribute budgets are not generous, and a tool list is unbounded. A
#: truncated list is worse than useless — it reads as "these are the tools" —
#: so an oversized list is dropped and said so, rather than clipped.
MAX_TOOLS_JSON_BYTES = 60_000


def _serialise_tools(tools: Any) -> str | None:
    """The tool definitions as JSON, or None with a reason logged."""
    if not tools:
        return None
    try:
        payload = json.dumps(tools, default=str, sort_keys=True)
    except (TypeError, ValueError) as e:
        logger.warning("tool definitions are not serialisable: %s", e)
        return None
    if len(payload.encode()) > MAX_TOOLS_JSON_BYTES:
        logger.warning(
            "tool definitions are %d bytes, over the %d limit; recording none "
            "rather than a truncated list that would read as complete",
            len(payload.encode()), MAX_TOOLS_JSON_BYTES)
        return None
    return payload


def _oversize(tools: Any) -> int | None:
    """The serialised size of a tool list that was refused for size, else None."""
    if not tools:
        return None
    try:
        size = len(json.dumps(tools, default=str, sort_keys=True).encode())
    except (TypeError, ValueError):
        return None
    return size if size > MAX_TOOLS_JSON_BYTES else None


def record_tools_on_current_span(kwargs: dict) -> bool:
    """Attach this request's tool definitions to the span already in progress.

    Returns whether anything was recorded, so the caller can tell "no tools were
    passed" from "there was no span to put them on" — different facts, and only
    the second is a defect.
    """
    tools = kwargs.get("tools")
    payload = _serialise_tools(tools)
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return False
    if payload is None:
        size = _oversize(tools)
        if size is None:
            return False
        # Say so on the span, not only in a log line nobody downstream reads:
        # the normaliser turns this into a TRUNCATED capture gap instead of
        # "the tool list did not reach the span".
        span.add_event(TOOLS_EVENT, attributes={
            "omitted": "size", "tools_bytes": size,
            "limit_bytes": MAX_TOOLS_JSON_BYTES})
        return False
    attributes: dict[str, Any] = {"tools": payload}
    tool_choice = kwargs.get("tool_choice")
    if tool_choice is not None:
        attributes["tool_choice"] = (
            tool_choice if isinstance(tool_choice, str)
            else json.dumps(tool_choice, default=str))
    span.add_event(TOOLS_EVENT, attributes=attributes)
    return True


#: (module, "Object.attribute", request shape) triples to supplement.
#: Chat-completions only: these are the calls that carry a tool list, and each
#: is the method the corresponding native instrumentor wraps. The same wrapper
#: records the verbatim request and the response's reasoning
#: (`_request_snapshot`), which need the same placement inside the
#: instrumentor's span for the same reason.
_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("openai.resources.chat.completions", "Completions.create", API_OPENAI_CHAT),
    ("openai.resources.chat.completions", "AsyncCompletions.create", API_OPENAI_CHAT),
    ("anthropic.resources.messages", "Messages.create", API_ANTHROPIC_MESSAGES),
    ("anthropic.resources.messages", "AsyncMessages.create", API_ANTHROPIC_MESSAGES),
)


#: Calls recorded verbatim only: the request and
#: the response in one ``redberry.llm.response.raw`` event, and no request or response
#: snapshot. The Responses API is the OpenAI Agents SDK's model call (OpenInference's
#: ``response`` LLM span is current when it runs); the normaliser reads it from this
#: record. ``Messages.stream`` is Anthropic's streaming helper, which does not go
#: through ``Messages.create``; for it the record is evidence only.
_RAW_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("openai.resources.responses", "Responses.create", API_OPENAI_RESPONSES, "call"),
    ("openai.resources.responses", "AsyncResponses.create", API_OPENAI_RESPONSES, "call"),
    ("anthropic.resources.messages", "Messages.stream", API_ANTHROPIC_MESSAGES, "manager"),
    ("anthropic.resources.messages", "AsyncMessages.stream", API_ANTHROPIC_MESSAGES, "manager"),
)

#: Targets already patched in this process: a second call (``init()`` twice, or
#: two tests) must not wrap a target again, or every event would be recorded twice.
_PATCHED: set[str] = set()


def enable_tool_definition_capture() -> list[str]:
    """Patch every available target, once per process. Returns the ones patched
    (now or by an earlier call).

    Failures are logged and skipped rather than raised: a missing tool list is
    a capture gap, and a capture gap must never take down the agent being
    observed.
    """
    import wrapt

    patched: list[str] = []
    for module, target, api in _TARGETS:
        root = module.split(".")[0]
        if importlib.util.find_spec(root) is None:
            continue
        if f"{module}.{target}" in _PATCHED:
            patched.append(f"{module}.{target}")
            continue
        try:
            @wrapt.patch_function_wrapper(module, target)
            def _wrapper(wrapped: Callable, instance: Any, args: tuple,
                         kwargs: dict, _api: str = api):
                try:
                    record_tools_on_current_span(kwargs)
                except Exception as e:                   # noqa: BLE001
                    logger.warning("failed to record tool definitions: %s", e)
                try:
                    record_request_on_current_span(kwargs, _api,
                                                   server_address=_server_address(instance))
                except Exception as e:                   # noqa: BLE001
                    logger.warning("failed to record the request snapshot: %s", e)
                result = wrapped(*args, **kwargs)
                if _wants_raw_response(kwargs):
                    span = trace.get_current_span()
                    return _on_parse(result, lambda parsed: _in_span(
                        span, _api, parsed, lambda: _handle_result(parsed, kwargs, _api)))
                if kwargs.get("stream") is True:
                    return _capture_stream(result, kwargs, _api)
                if inspect.isawaitable(result):
                    return _record_after(result, kwargs, _api)
                _record_response(result, kwargs, _api)
                return result

            patched.append(f"{module}.{target}")
            _PATCHED.add(f"{module}.{target}")
        except (ImportError, AttributeError) as e:
            logger.debug("no tool-definition capture for %s.%s: %s",
                         module, target, e)
    for module, target, api, kind in _RAW_TARGETS:
        if importlib.util.find_spec(module.split(".")[0]) is None:
            continue
        if f"{module}.{target}" in _PATCHED:
            patched.append(f"{module}.{target}")
            continue
        try:
            @wrapt.patch_function_wrapper(module, target)
            def _raw_wrapper(wrapped: Callable, instance: Any, args: tuple, kwargs: dict,
                             _api: str = api, _kind: str = kind):
                span = trace.get_current_span()
                recording = span is not None and span.is_recording() and _is_llm_span(span)
                result = wrapped(*args, **kwargs)
                if not recording:
                    return result
                if _kind == "call" and _wants_raw_response(kwargs):
                    return _on_parse(result, lambda parsed: _record_raw_call(parsed, span, kwargs, _api, _kind))
                return _record_raw_call(result, span, kwargs, _api, _kind)

            patched.append(f"{module}.{target}")
            _PATCHED.add(f"{module}.{target}")
        except (ImportError, AttributeError) as e:
            logger.debug("no verbatim capture for %s.%s: %s", module, target, e)
    return patched


def _server_address(instance: Any) -> str | None:
    """The host of the client a resource belongs to (``Completions._client.base_url``).

    The normaliser derives the provider from it: the openai SDK pointed at
    api.fireworks.ai is a Fireworks call, though its instrumentor says "openai".
    """
    try:
        base_url = getattr(getattr(instance, "_client", None), "base_url", None)
        host = getattr(base_url, "host", None)
        if host is None and isinstance(base_url, str):
            from urllib.parse import urlparse
            host = urlparse(base_url).hostname
        return str(host) if host else None
    except Exception:                                    # noqa: BLE001
        return None


def _record_response(result: Any, kwargs: dict, api: str) -> None:
    # Still inside the instrumentor's span: it ends the span only after this
    # wrapper returns (async: after the awaitable below completes).
    try:
        record_response_on_current_span(result, kwargs, api)
    except Exception as e:                               # noqa: BLE001
        logger.warning("failed to record the response reasoning: %s", e)


async def _record_after(awaitable: Any, kwargs: dict, api: str) -> Any:
    result = await awaitable
    _record_response(result, kwargs, api)
    return result


def _record_raw_call(result: Any, span: Any, kwargs: dict, api: str, kind: str) -> Any:
    """Record one evidence-only call (``_RAW_TARGETS``) and hand its result back
    untouched, or passed through when it is a stream. A failure is a capture gap,
    logged, never the caller's error."""
    try:
        request = request_record(kwargs)
        if kind == "manager":
            return capture_message_stream(result, span, request)
        if kwargs.get("stream") is True:
            if inspect.isawaitable(result):
                async def awaited_stream() -> Any:
                    return capture_stream(await result, span, api, raw_only=True, request=request)
                return awaited_stream()
            return capture_stream(result, span, api, raw_only=True, request=request)

        def record(response: Any) -> None:
            if not span.is_recording():
                logger.warning("an LLM response (%s) was read after its span ended: not recorded", api)
                return
            try:
                span.add_event(RAW_EVENT, attributes=build_raw_attributes(api, response=response, request=request))
            except Exception as e:                       # noqa: BLE001
                logger.warning("failed to record a response verbatim: %s", e)

        if inspect.isawaitable(result):
            async def awaited() -> Any:
                response = await result
                record(response)
                return response
            return awaited()
        record(result)
        return result
    except Exception as e:                               # noqa: BLE001
        logger.warning("failed to set up verbatim capture: %s", e)
        return result


# ----------------------------------------------------------------------
# openai's raw-response wrappers
# ----------------------------------------------------------------------
#
# ``client.x.with_raw_response.create(...)`` and ``with_streaming_response.create(...)`` call the
# same ``create`` with this header, and it returns the HTTP response instead of the result; the
# caller gets the result from the response's ``parse()``. The OpenAI Agents SDK streams the
# Responses API this way. Recording ``create``'s return value recorded the HTTP response (a repr),
# and wrapping it as a stream hid the real stream and closed an async response without awaiting it.

_RAW_RESPONSE_HEADER = "x-stainless-raw-response"


def _wants_raw_response(kwargs: dict) -> bool:
    headers = kwargs.get("extra_headers")
    return isinstance(headers, Mapping) and any(str(k).lower() == _RAW_RESPONSE_HEADER for k in headers)


class _ParsedPassThrough:
    """The HTTP response of a raw-response call, passed through. What ``parse()`` returns goes to
    ``handle``, as the call's own result would have (a stream passed through and recorded, a
    response recorded); everything else (``headers``, ``close``, ``request_id``) is the response's."""

    def __init__(self, inner: Any, handle: Callable[[Any], Any]) -> None:
        self._inner, self._handle, self._parsed = inner, handle, None

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        parsed = self._inner.parse(*args, **kwargs)
        if args or kwargs:          # parse(to=...): a type of the caller's choosing, not the result
            return parsed
        if inspect.isawaitable(parsed):
            async def awaited() -> Any:
                return self._once(await parsed)
            return awaited()
        return self._once(parsed)

    def _once(self, parsed: Any) -> Any:
        # openai caches parse(): the same result comes back as the same pass-through, recorded once
        if self._parsed is None or self._parsed[0] is not parsed:
            try:
                self._parsed = (parsed, self._handle(parsed))
            except Exception as e:                       # noqa: BLE001 - a capture gap, never the caller's error
                logger.warning("failed to record a parsed LLM response: %s", e)
                self._parsed = (parsed, parsed)
        return self._parsed[1]

    def __getattr__(self, name: str) -> Any:
        if name in ("_inner", "_handle", "_parsed"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def _on_parse(result: Any, handle: Callable[[Any], Any]) -> Any:
    """``result`` (awaited, if it is awaitable) as a :class:`_ParsedPassThrough`."""
    if inspect.isawaitable(result):
        async def awaited() -> Any:
            return _ParsedPassThrough(await result, handle)
        return awaited()
    return _ParsedPassThrough(result, handle)


def _in_span(span: Any, api: str, parsed: Any, record: Callable[[], Any]) -> Any:
    """Run ``record`` with the model call's span current again: ``parse()`` comes after ``create``
    returned, outside the span. A span already ended takes no events; that is logged."""
    if span is None or not span.is_recording():
        logger.warning("an LLM response (%s) was read after its span ended: not recorded", api)
        return parsed
    with trace.use_span(span, end_on_exit=False):
        return record()


def _handle_result(result: Any, kwargs: dict, api: str) -> Any:
    """What the wrapper does with a call's result: a stream passed through, a response recorded."""
    if kwargs.get("stream") is True:
        return _capture_stream(result, kwargs, api)
    _record_response(result, kwargs, api)
    return result


def _capture_stream(result: Any, kwargs: dict, api: str) -> Any:
    """A streamed call's result, passed through and recorded as it is consumed
    (``_stream_capture``). The span is taken now, while the model call's span is
    current; the caller iterates later, outside it. If the pass-through cannot be
    put in place, the stream goes back untouched and the absence is recorded."""
    span = trace.get_current_span()
    if span is None or not span.is_recording() or not _is_llm_span(span):
        return result

    def wrap(stream: Any) -> Any:
        try:
            return capture_stream(stream, span, api)
        except Exception as e:                           # noqa: BLE001
            logger.warning("failed to wrap a streamed response: %s", e)
            _record_response(stream, kwargs, api)        # omitted = "stream"
            return stream

    if inspect.isawaitable(result):
        async def awaited() -> Any:
            return wrap(await result)
        return awaited()
    return wrap(result)
