"""``internal_call()``: mark the model calls an agent makes for itself.

A control's verifier, a judge, a planner or a summariser is a model call whose output stays inside
the agent. Recorded like any other call, its text became a message to the user: RAP's G3 verifier
put 11 ``ACCEPT`` / ``REJECT`` "replies" into one airline trace. Wrap such a call::

    with redberry_sdk.internal_call("verifier"):
        resp = client.chat.completions.create(...)

and every span started inside the block carries ``redberry.call_role = "internal"`` and
``redberry.call_name = "verifier"``. The normaliser then records the call under the trace's
``metadata.internal_calls`` and not as an event (``redberry_normaliser._call_role``).

Mark only calls whose output the agent consumes itself. A call whose tool calls the agent
dispatches is the agent acting, and must not be marked.

The marker is a ``contextvars`` value, so it follows the call into ``await`` and stays out of
other threads and tasks. Without ``redberry_sdk.init()`` it does nothing, and costs nothing.

``redberry.*``, not ``valp.*``: ``valp.*`` is VALP's frozen evidence vocabulary
(``redberry_sdk.valp``), and a new key there is a VALP specification change.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

#: Read by ``redberry_normaliser._call_role``; kept in step with it.
CALL_ROLE_ATTR = "redberry.call_role"
CALL_NAME_ATTR = "redberry.call_name"
INTERNAL = "internal"

_role: contextvars.ContextVar[tuple[str, str | None] | None] = contextvars.ContextVar(
    "redberry_call_role", default=None)


@contextmanager
def internal_call(name: str | None = None) -> Iterator[None]:
    """Mark every span started inside the block as an internal call named ``name``."""
    if name is not None and not isinstance(name, str):
        raise TypeError(f"internal_call name must be a string, got {type(name).__name__}")
    token = _role.set((INTERNAL, name))
    try:
        yield
    finally:
        _role.reset(token)


def current_call_role() -> tuple[str, str | None] | None:
    """``(role, name)`` inside ``internal_call()``, else None."""
    return _role.get()


class CallRoleSpanProcessor(SpanProcessor):
    """Stamps ``redberry.call_role`` / ``redberry.call_name`` at span start inside ``internal_call()``."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        role = _role.get()
        if role is None:
            return
        attrs = span.attributes or {}
        if CALL_ROLE_ATTR not in attrs:
            span.set_attribute(CALL_ROLE_ATTR, role[0])
        if role[1] and CALL_NAME_ATTR not in attrs:
            span.set_attribute(CALL_NAME_ATTR, role[1])

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
