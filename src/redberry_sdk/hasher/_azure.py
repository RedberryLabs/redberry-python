"""Azure Functions (Python v2 programming model): ``register()`` and ``with_redberry``.

::

    import azure.functions as func
    from redberry_sdk.hasher import register, with_redberry

    register()                      # once, at import: tracer provider, hasher, LLM instrumentation
    app = func.FunctionApp()

    @app.route(route="refund")
    @with_redberry                  # innermost, right above the handler
    def refund(req: func.HttpRequest) -> func.HttpResponse:
        ...

On AWS Lambda (Python), the same two, on the handler Lambda calls with ``(event, context)``::

    from redberry_sdk.hasher import register, with_redberry

    register()

    @with_redberry(agent_id="AGT-...")
    def lambda_handler(event, context):
        ...

``with_redberry`` opens a root span for the invocation, runs the handler (sync or async), then finalises
that trace and completes the store and the signed report before the handler's result is returned, so
nothing is left to a background thread the host may freeze (Lambda freezes the process as soon as the
handler returns). That wait is bounded (``timeout``, default 15 s, and on Lambda also by the
invocation's remaining time); past it the handler returns and the work carries on in the background.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import threading
import urllib.parse
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self, TypeVar, overload

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Link, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ._agents import is_agent_id, resolve_agent
from ._config import HasherConfig
from ._envelope import AGENT_ATTRIBUTE
from ._processor import RedberryProcessor, TraceResult
from ._span import span_id_hex, trace_id_hex
from .. import _env

__all__ = ["DEFAULT_TIMEOUT", "get_processor", "register", "with_redberry"]

log = logging.getLogger("redberry.hasher")
DEFAULT_TIMEOUT = 15.0
_TRACER_NAME = "redberry_sdk.hasher"

_lock = threading.Lock()
_processor: RedberryProcessor | None = None
_provider: TracerProvider | None = None

F = TypeVar("F", bound=Callable[..., Any])


def get_processor() -> RedberryProcessor | None:
    """The processor ``register()`` installed, if any."""
    return _processor


def register(*, instrument: bool = True, capture_content: bool = True,
             allow_missing_instrumentation: bool | None = None, allow_content_capture_off: bool | None = None,
             processor: RedberryProcessor | None = None, **options: Any) -> RedberryProcessor:
    """Install the hasher: a ``RedberryProcessor`` on the global tracer provider (one is created, with this
    function app's resource, unless an SDK provider is already set, e.g. by Azure Monitor's distro, in
    which case the processor is added to it), and the SDK's LLM instrumentation (``redberry_sdk.init()``'s:
    OpenAI and Azure OpenAI, Anthropic, and every other installed provider), so model calls inside a
    handler produce spans. Idempotent: a second call returns the first processor.

    Like ``init()``, it refuses to start (``MissingInstrumentationError``, ``ContentCaptureDisabledError``)
    when a provider library is installed without its instrumentation or content capture is off, unless
    allowed (``allow_missing_instrumentation`` / ``REDBERRY_ALLOW_MISSING_INSTRUMENTATION=1``,
    ``allow_content_capture_off`` / ``REDBERRY_ALLOW_CONTENT_CAPTURE_OFF=1``): a run hashed without its
    model calls cannot be priced. When the hasher is disabled (not on Azure, ``REDBERRY_DISABLED=1``, or
    misconfigured), nothing is installed. ``options`` are ``RedberryProcessor``'s."""
    global _processor, _provider
    with _lock:
        if _processor is not None:
            return _processor
        proc = processor or RedberryProcessor(**options)
        if not proc.enabled:
            log.info("[redberry] hasher not installed: %s", proc.cfg.disabled_reason)
            _processor = proc
            return proc
        if instrument:
            from .._sdk import enable_instrumentation, prepare_instrumentation

            allow_missing = (allow_missing_instrumentation if allow_missing_instrumentation is not None
                             else _env.get("REDBERRY_ALLOW_MISSING_INSTRUMENTATION") == "1")
            allow_off = (allow_content_capture_off if allow_content_capture_off is not None
                         else _env.get("REDBERRY_ALLOW_CONTENT_CAPTURE_OFF") == "1")
            prepare_instrumentation(capture_content=capture_content, allow_missing_instrumentation=allow_missing,
                                    allow_content_capture_off=allow_off)
        from .._call_role import CallRoleSpanProcessor

        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            provider = current
        else:
            provider = TracerProvider(resource=Resource.create(_resource_attributes()))
            trace.set_tracer_provider(provider)
        # redberry.call_role on the calls an agent makes for itself (redberry_sdk.internal_call), so the
        # normaliser does not read them as replies when the stored run is priced.
        provider.add_span_processor(CallRoleSpanProcessor())
        provider.add_span_processor(proc)
        _provider = provider
        if instrument:
            enable_instrumentation(allow_missing_instrumentation=allow_missing)
        _processor = proc
        return proc


def _resource_attributes() -> dict[str, Any]:
    function = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if function:
        attrs: dict[str, Any] = {"cloud.provider": "aws", "cloud.platform": "aws_lambda", "service.name": function,
                                 "faas.name": function}
        if region := os.environ.get("AWS_REGION"):
            attrs["cloud.region"] = region
        if version := os.environ.get("AWS_LAMBDA_FUNCTION_VERSION"):
            attrs["faas.version"] = version
        return attrs
    attrs = {"cloud.provider": "azure", "cloud.platform": "azure_functions"}
    site = os.environ.get("WEBSITE_SITE_NAME")
    if site:
        attrs["service.name"] = site
        attrs["faas.name"] = site
    if region := os.environ.get("REGION_NAME"):
        attrs["cloud.region"] = region
    return attrs


@overload
def with_redberry(handler: F, *, agent_id: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> F: ...


@overload
def with_redberry(handler: None = None, *, agent_id: str | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> Callable[[F], F]: ...


def with_redberry(handler: Callable[..., Any] | None = None, *, agent_id: str | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Hash, store and report each invocation of an Azure Functions or AWS Lambda handler (sync or async).

    ``@with_redberry`` or ``@with_redberry(agent_id="AGT-...")``: ``agent_id`` is the agent this handler's
    runs belong to, over ``REDBERRY_AGENTS`` / ``REDBERRY_AGENT_ID``. The invocation's root span carries
    ``faas.name``, ``faas.trigger``, ``faas.invocation_id`` (when the handler takes the ``context``),
    ``redberry.agent_id`` and, for HTTP, ``http.request.method``, ``url.path`` (never the query string,
    which can hold the function key) and ``http.response.status_code``. On Lambda (``handler(event,
    context)``): ``faas.name`` (the function), ``faas.invocation_id`` (``context.aws_request_id``),
    ``faas.trigger`` (``http`` for API Gateway and function URL events, else ``other``),
    ``cloud.provider=aws``, ``cloud.platform=aws_lambda``, ``cloud.region`` and ``cloud.resource_id``. ``timeout``: the most seconds the
    handler's return waits for the store and the report. The handler's signature is kept (the Functions
    host binds by parameter name), and its exceptions propagate unchanged, after the run is reported."""
    if agent_id is not None and not is_agent_id(agent_id):
        raise ValueError(f"with_redberry: agent_id must be AGT- and a ULID, not {agent_id!r}")

    def decorate(fn: F) -> F:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                proc = _ensure_registered()
                if not proc.enabled:
                    return await fn(*args, **kwargs)
                with _Invocation(proc, agent_id, fn, args, kwargs) as inv:
                    inv.result = await fn(*args, **kwargs)
                await asyncio.get_running_loop().run_in_executor(None, inv.complete, inv.budget(timeout))
                return inv.outcome()
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            proc = _ensure_registered()
            if not proc.enabled:
                return fn(*args, **kwargs)
            with _Invocation(proc, agent_id, fn, args, kwargs) as inv:
                inv.result = fn(*args, **kwargs)
            inv.complete(inv.budget(timeout))
            return inv.outcome()
        return wrapper  # type: ignore[return-value]

    return decorate(handler) if handler is not None else decorate


def _ensure_registered() -> RedberryProcessor:
    """``register()`` on first use when the app did not call it. A refusal there (missing instrumentation,
    content capture off) is logged and the handler runs unhashed, rather than failing every invocation;
    call ``register()`` at import to have it stop the app instead."""
    global _processor
    if _processor is not None:
        return _processor
    try:
        return register()
    except Exception as e:  # noqa: BLE001 - never into the customer's code
        log.error("[redberry] hasher not installed: %s", e)
        with _lock:
            if _processor is None:
                _processor = RedberryProcessor(config=HasherConfig(enabled=False, disabled_reason=str(e)))
            return _processor


class _Invocation:
    """The invocation's root span: opened on enter, ended on exit (with the handler's error, if any)."""

    def __init__(self, proc: RedberryProcessor, agent_id: str | None, fn: Callable[..., Any], args: tuple[Any, ...],
                 kwargs: dict[str, Any]) -> None:
        self.proc, self.agent_id, self.fn = proc, agent_id, fn
        self.values = [*args, *kwargs.values()]
        self.result: Any = None
        self.error: BaseException | None = None
        self.trace_id: str | None = None
        self.span_id: str | None = None
        self._cm: Any = None
        self.lambda_ctx: Any = None

    def __enter__(self) -> Self:
        self.lambda_ctx = next((v for v in self.values if _is_lambda_context(v)), None)
        req = ctx = None
        attrs: dict[str, Any]
        if self.lambda_ctx is not None:
            event = self.values[0] if self.values and self.values[0] is not self.lambda_ctx else None
            attrs = _lambda_attributes(event, self.lambda_ctx)
            http = "http.request.method" in attrs
        else:
            req = next((v for v in self.values if _is_http_request(v)), None)
            ctx = next((v for v in self.values if _is_function_context(v)), None)
            name = str(getattr(ctx, "function_name", None) or getattr(self.fn, "__name__", None) or "handler")
            attrs = {"faas.name": name, "faas.trigger": _trigger(self.values, req)}
            if ctx is not None and getattr(ctx, "invocation_id", None):
                attrs["faas.invocation_id"] = str(ctx.invocation_id)
            if req is not None:
                attrs["http.request.method"] = str(req.method)
                # The path only: the query string can hold the function key (?code=...).
                path = urllib.parse.urlsplit(str(req.url)).path
                if path:
                    attrs["url.path"] = path
            http = req is not None
        name = str(attrs["faas.name"])
        cfg = self.proc.cfg
        agent = self.agent_id or resolve_agent(attrs, cfg.agents_by_route, cfg.agent_id)
        if is_agent_id(agent):
            attrs[AGENT_ATTRIBUTE] = agent
        # A trace of its own, so this invocation is one stored run whatever surrounds it. The host's trace
        # (Application Insights) and any span already active are linked, not parented: their sampling
        # must not decide whether this run is hashed.
        host = _host_span_context(ctx) if self.lambda_ctx is None else None
        links = [Link(sc) for sc in (host, trace.get_current_span().get_span_context())
                 if sc is not None and sc.is_valid]
        tracer = (_provider or trace.get_tracer_provider()).get_tracer(_TRACER_NAME)
        self._cm = tracer.start_as_current_span(
            name, context=otel_context.Context(), kind=SpanKind.SERVER if http else SpanKind.INTERNAL,
            attributes=attrs, links=links, record_exception=False, set_status_on_exception=False, end_on_exit=True)
        span = self._cm.__enter__()
        sc = span.get_span_context()
        if sc.is_valid:
            self.trace_id, self.span_id = trace_id_hex(sc.trace_id), span_id_hex(sc.span_id)
        self.span = span
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> bool:
        if exc is not None and not isinstance(exc, Exception):  # cancellation, exit: not waited on
            self._cm.__exit__(exc_type, exc, tb)
            return False
        if exc is not None:
            self.error = exc
            self.span.record_exception(exc)
            self.span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
        else:
            status = getattr(self.result, "status_code", None)
            if status is None and isinstance(self.result, dict):         # Lambda proxy reply {"statusCode": …}
                status = self.result.get("statusCode")
            if isinstance(status, int) and not isinstance(status, bool):
                self.span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    self.span.set_status(Status(StatusCode.ERROR))
        self._cm.__exit__(None, None, None)
        return True  # re-raised by outcome(), after the run is reported

    def budget(self, timeout: float) -> float:
        """``timeout``, and on Lambda no more than the invocation has left (less a second to return in)."""
        remaining = getattr(self.lambda_ctx, "get_remaining_time_in_millis", None)
        if callable(remaining):
            try:
                left = float(remaining()) / 1000.0 - 1.0
            except (TypeError, ValueError):
                return timeout
            return max(0.1, min(timeout, left))
        return timeout

    def complete(self, timeout: float) -> TraceResult | None:
        if self.trace_id is None or self.proc.root_span_id(self.trace_id) != self.span_id:
            return None  # not sampled, or not tracked (the per-trace caps)
        try:
            result = self.proc.complete_trace(self.trace_id, agent_id=self.agent_id, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - never into the customer's code
            log.error("[redberry] trace %s: %s", self.trace_id, e)
            return None
        if result is None:
            log.warning("[redberry] trace %s: store and report not finished within %.1f s; continuing in the "
                        "background", self.trace_id, timeout)
        return result

    def outcome(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


def _trigger(values: list[Any], req: Any) -> str:
    """OpenTelemetry's ``faas.trigger`` for the binding the handler was given."""
    if req is not None:
        return "http"
    for v in values:
        if hasattr(v, "past_due"):                                    # TimerRequest
            return "timer"
        if hasattr(v, "get_body") and any(hasattr(v, a) for a in ("dequeue_count", "enqueued_time",
                                                                  "delivery_count", "sequence_number")):
            return "pubsub"                                           # queue, Service Bus, Event Hubs
        if hasattr(v, "read") and hasattr(v, "uri"):                  # blob InputStream
            return "datasource"
    return "other"


def _is_lambda_context(v: Any) -> bool:
    """The context AWS Lambda's Python runtime passes as a handler's second argument."""
    return hasattr(v, "aws_request_id") and hasattr(v, "function_name")


def _lambda_attributes(event: Any, ctx: Any) -> dict[str, Any]:
    """The root span's attributes for a Lambda invocation: faas.*, cloud.*, and HTTP for API Gateway
    (REST: ``httpMethod``; HTTP API and function URLs: ``requestContext.http``)."""
    function = str(getattr(ctx, "function_name", "") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "handler")
    attrs: dict[str, Any] = {"faas.name": function, "faas.trigger": "other", "cloud.provider": "aws",
                             "cloud.platform": "aws_lambda"}
    if getattr(ctx, "aws_request_id", None):
        attrs["faas.invocation_id"] = str(ctx.aws_request_id)
    arn = str(getattr(ctx, "invoked_function_arn", "") or "")
    parts = arn.split(":")
    region = os.environ.get("AWS_REGION") or (parts[3] if len(parts) > 4 and parts[2] == "lambda" else "")
    if region:
        attrs["cloud.region"] = region
    if arn:
        attrs["cloud.resource_id"] = arn
        if len(parts) > 4 and parts[4]:
            attrs["cloud.account.id"] = parts[4]
    if isinstance(event, dict):
        rc = event.get("requestContext") if isinstance(event.get("requestContext"), dict) else {}
        http = rc.get("http") if isinstance(rc.get("http"), dict) else None
        method = (http or {}).get("method") or event.get("httpMethod")
        if method:
            attrs["faas.trigger"] = "http"
            attrs["http.request.method"] = str(method)
            # The path only, never the query string.
            path = event.get("rawPath") or (http or {}).get("path") or event.get("path")
            if path:
                attrs["url.path"] = urllib.parse.urlsplit(str(path)).path
    return attrs


def _is_http_request(v: Any) -> bool:
    return all(hasattr(v, a) for a in ("method", "url", "get_body", "route_params"))


def _is_function_context(v: Any) -> bool:
    return hasattr(v, "invocation_id") and hasattr(v, "function_name")


def _host_span_context(ctx: Any) -> trace.SpanContext | None:
    tc = getattr(ctx, "trace_context", None)
    parent = getattr(tc, "trace_parent", None) or getattr(tc, "Traceparent", None)
    if not parent:
        return None
    carrier = {"traceparent": str(parent)}
    state = getattr(tc, "trace_state", None) or getattr(tc, "Tracestate", None)
    if state:
        carrier["tracestate"] = str(state)
    sc = trace.get_current_span(TraceContextTextMapPropagator().extract(carrier)).get_span_context()
    return sc if sc.is_valid else None
