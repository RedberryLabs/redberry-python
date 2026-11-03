"""A small HTTP client on the standard library, with ``@redberrylabs/otel``'s retry rule.

5xx, 429 and network errors are retried (3 attempts in all, waiting 0.5 s and then 2 s); every other
reply is final. The transport is a plain callable so tests (and callers with their own client) can
replace it: ``transport(Request) -> Response``, raising ``OSError`` for a network failure.

Tracing is suppressed around each request, so an instrumented ``urllib`` adds no spans to the trace
being hashed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import context as otel_context

__all__ = [
    "DEFAULT_RETRY_DELAYS",
    "Attempted",
    "Request",
    "Response",
    "Transport",
    "describe",
    "send",
    "trim_base",
    "urllib_transport",
    "with_retries",
]

DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0)
DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


Transport = Callable[[Request], Response]


def urllib_transport(req: Request) -> Response:
    r = urllib.request.Request(req.url, data=req.body, method=req.method, headers=req.headers)
    try:
        with urllib.request.urlopen(r, timeout=req.timeout) as resp:
            return Response(resp.status, resp.read(), dict(resp.headers.items()))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except OSError:
            body = b""
        return Response(e.code, body, dict(e.headers.items()) if e.headers else {})


def _suppressed(transport: Transport, req: Request) -> Response:
    ctx = otel_context.set_value(otel_context._SUPPRESS_INSTRUMENTATION_KEY, True)
    http_key = getattr(otel_context, "_SUPPRESS_HTTP_INSTRUMENTATION_KEY", None)
    if http_key is not None:
        ctx = otel_context.set_value(http_key, True, ctx)
    token = otel_context.attach(ctx)
    try:
        return transport(req)
    finally:
        otel_context.detach(token)


def retryable(status: int) -> bool:
    return status >= 500 or status == 429


@dataclass
class Attempted:
    attempts: int = 0
    response: Response | None = None
    error: BaseException | None = None


def with_retries(send_once: Callable[[], Response], delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                 sleep: Callable[[float], None] = time.sleep) -> Attempted:
    """Call ``send_once`` until a reply that is not retryable, at most ``len(delays) + 1`` times."""
    last = Attempted()
    for i in range(len(delays) + 1):
        if i:
            sleep(delays[i - 1])
        try:
            response = send_once()
        except OSError as e:  # URLError, timeouts, connection resets
            last = Attempted(i + 1, None, e)
            continue
        last = Attempted(i + 1, response, None)
        if not retryable(response.status):
            return last
    return last


def send(transport: Transport, req: Request, delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
         sleep: Callable[[float], None] = time.sleep) -> Attempted:
    return with_retries(lambda: _suppressed(transport, req), delays, sleep)


def describe(r: Response) -> str:
    text = r.body[:300].decode("utf-8", "replace") if r.body else ""
    return f"HTTP {r.status}" + (f": {text}" if text else "")


def trim_base(url: str) -> str:
    """Strip a trailing slash so ``{base}/v1/...`` never doubles it."""
    return url.rstrip("/")
