"""First-party ``valp.*`` span attributes: the rating signals standard OpenTelemetry cannot carry.

VALP freezes these values into signed evidence, so the one rule that matters more than any other:
**omit rather than guess.** Every helper here leaves an attribute off when it is not told the
value, and refuses a value outside the vocabulary rather than emitting it. A defaulted
``autonomous`` would understate supervision; a defaulted ``human_approved`` would overstate it.

The attributes (VALP ``spec/schema/annotations.v1.json`` gives the types and values):

================================  ======================================  ================================
Attribute                         Values                                  Set on
================================  ======================================  ================================
``valp.interaction_id``           ULID                                    every span in an interaction
``valp.authority``                autonomous, human_approved,             tool calls
                                  human_preapproved, human_initiated
``valp.is_mutating``              bool                                    tool calls
``valp.is_external``              bool                                    tool calls
``valp.approval_outcome``         granted, denied, timeout                approval decisions
``valp.approval_scope``           call, standing                          approval decisions
``valp.guardrail_action``         block, modify, warn                     guardrail trips
``valp.context_class``            ASCII, at most 32 characters            inference spans
``valp.disposition``              completed, abandoned, failed,           the interaction span
                                  escalated
``valp.previous_interaction_id``  ULID                                    an interaction resumed after
                                                                          the one it continues expired
``valp.operation``                human_approval_request,                 VALP's own event spans
                                  human_approval_decision, guardrail_trip
``valp.emitter_id``               ULID, one per process                   every exported span
``valp.span_seq``                 0, 1, 2, … contiguous per emitter       every exported span
``valp.tool_ran``                 false; omitted when the tool ran        a tool span in which the tool
                                                                          never ran (set at export)
================================  ======================================  ================================

``emitter_id`` and ``span_seq`` are stamped at EXPORT (``ValpSequencingExporter``), in the order
spans leave the process, so a missing number at the VALP sink is a span lost on the way (a full
collector queue, a restart without file storage, a span the sink refused), never a long-running
span that has not ended yet.

A span carrying ``valp.tool_ran`` reaches VALP like any other (its collector pipeline forwards full
spans), and VALP reads it as OTHER: a tool that never ran made no tool call. The normaliser keeps
it as a control that worked.

``human_approved`` means a person approved THIS call; ``human_preapproved`` means the call ran
under a standing approval ("always approve this tool") or one the SDK did not see being given. The
two are separate values, not one value plus a flag, so a reader that does not know the second
cannot quietly count it as a per-call review. ``approval_scope`` says the same of a decision:
``standing`` covers future calls too.

``valp.*``, not ``gen_ai.*``: the GenAI namespace belongs to the OpenTelemetry working group and is
still in Development status, and a future ``gen_ai.authority`` with other semantics would silently
change the meaning of evidence already frozen.

``is_mutating`` and ``is_external`` come from the tool catalogue the insured **confirmed** at
binding (``ToolCatalogue``), never from unconfirmed manifest output. ``authority`` and
``approval_outcome`` are per call, so only the agent's control flow knows them: the helpers below
(and ``redberry_sdk.integrations.openai_agents``) are the two integration points per framework,
tool dispatch and the approval primitive.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

__all__ = [
    "AUTHORITIES", "APPROVAL_OUTCOMES", "GUARDRAIL_ACTIONS", "DISPOSITIONS",
    "APPROVAL_SCOPES", "VOCABULARY", "ToolCatalogue", "ValpSpanProcessor", "new_ulid",
    "interaction", "bind_interaction", "end_interaction", "current_interaction_id", "tool_call",
    "approval_request", "approval_decision", "guardrail_trip", "set_context_class",
    "configure", "settings", "current_catalogue", "ValpSequencingExporter",
    "ValpToolRanMarker", "watch_tool", "tool_ran",
]

AUTHORITIES = frozenset({"autonomous", "human_approved", "human_preapproved", "human_initiated"})
APPROVAL_OUTCOMES = frozenset({"granted", "denied", "timeout"})
APPROVAL_SCOPES = frozenset({"call", "standing"})
GUARDRAIL_ACTIONS = frozenset({"block", "modify", "warn"})
DISPOSITIONS = frozenset({"completed", "abandoned", "failed", "escalated"})

A_INTERACTION = "valp.interaction_id"
A_AUTHORITY = "valp.authority"
A_MUTATING = "valp.is_mutating"
A_EXTERNAL = "valp.is_external"
A_OUTCOME = "valp.approval_outcome"
A_GUARDRAIL = "valp.guardrail_action"
A_CONTEXT_CLASS = "valp.context_class"
A_DISPOSITION = "valp.disposition"
A_OPERATION = "valp.operation"
A_APPROVAL_SCOPE = "valp.approval_scope"
A_PREVIOUS_INTERACTION = "valp.previous_interaction_id"
A_EMITTER = "valp.emitter_id"
A_SPAN_SEQ = "valp.span_seq"
A_TOOL_RAN = "valp.tool_ran"

# Every valp.* attribute the SDK emits, and no other, bar valp.tool_ran (set at export, below). VALP's
# interpreter reads these and the metadata keys redberry_normaliser.dialects reads; it stores whole
# spans, so nothing downstream keeps a list of them.
VOCABULARY: frozenset[str] = frozenset({
    A_INTERACTION, A_AUTHORITY, A_MUTATING, A_EXTERNAL, A_OUTCOME, A_GUARDRAIL, A_CONTEXT_CLASS,
    A_DISPOSITION, A_OPERATION, A_APPROVAL_SCOPE, A_PREVIOUS_INTERACTION, A_EMITTER, A_SPAN_SEQ,
    A_TOOL_RAN,
})

_SETTINGS: dict[str, Any] = {}
_PROCESSORS: list["ValpSpanProcessor"] = []  # the installed processor(s): where the catalogue lives


def configure(**settings: Any) -> None:
    """Settings ``init()`` passes down from ``SDKConfig`` (``pending_approval_ttl_s``)."""
    _SETTINGS.update({k: v for k, v in settings.items() if v is not None})


def settings() -> dict[str, Any]:
    return dict(_SETTINGS)

_TRACER_NAME = "redberry.valp"
_interaction_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("valp_interaction_id", default=None)


# ── ULIDs ────────────────────────────────────────────────────────────────────────────────────

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A canonical 26-character uppercase ULID: 48 bits of milliseconds, 80 random bits."""
    n = (int(time.time() * 1000) & ((1 << 48) - 1)) << 80 | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


# ── the confirmed tool catalogue ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CatalogueEntry:
    is_external: bool
    is_mutating: bool | None  # None when mutation varies per call: only the call site knows


class ToolCatalogue:
    """The tool catalogue the insured confirmed at binding, as VALP's binding step writes it.

    Format (``tool_catalogue.confirmed.json``)::

        {"v": 1, "tools": [{"name": "issue_refund", "is_external": true,
                            "is_mutating": true, "mutation": "fixed"}, ...]}

    ``mutation`` is ``"fixed"`` (``is_mutating`` holds for every call) or ``"per_call"``
    (``is_mutating`` is absent and must come from the tool wrapper, or be omitted). VALP hashes
    this derived catalogue into every block header as ``tool_catalogue_hash``, so the values set
    from it are ones the insured signed off, not the manifest extractor's guesses.
    """

    def __init__(self, entries: dict[str, CatalogueEntry]) -> None:
        self._entries = dict(entries)

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> "ToolCatalogue":
        if doc.get("v") != 1 or not isinstance(doc.get("tools"), list):
            raise ValueError("not a v1 confirmed tool catalogue")
        entries: dict[str, CatalogueEntry] = {}
        for t in doc["tools"]:
            name, ext, mutation = t.get("name"), t.get("is_external"), t.get("mutation")
            if not isinstance(name, str) or not name or not isinstance(ext, bool):
                raise ValueError(f"bad catalogue entry: {t!r}")
            if mutation == "fixed" and isinstance(t.get("is_mutating"), bool):
                entries[name] = CatalogueEntry(ext, t["is_mutating"])
            elif mutation == "per_call" and "is_mutating" not in t:
                entries[name] = CatalogueEntry(ext, None)
            else:
                raise ValueError(f"bad catalogue entry: {t!r}")
        return cls(entries)

    @classmethod
    def load(cls, path: str | Path) -> "ToolCatalogue":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, tool_name: str) -> CatalogueEntry | None:
        return self._entries.get(tool_name)


# ── the span processor ───────────────────────────────────────────────────────────────────────


class ValpSpanProcessor(SpanProcessor):
    """Stamps ``valp.*`` values the SDK can know at span start.

    - ``valp.interaction_id`` on every span started inside ``interaction()``, including the spans
      other instrumentations create (the model calls), so every record groups into its interaction.
    - ``valp.is_external`` / ``valp.is_mutating`` on ``execute_tool`` spans, from the confirmed
      catalogue, by ``gen_ai.tool.name``. A tool the catalogue does not list gets nothing; a
      per-call tool gets ``is_external`` only. An attribute already on the span is never replaced.

    Attributes set after a span starts are not seen here (an ended span is read-only); the helpers
    below set their own.
    """

    def __init__(self, catalogue: ToolCatalogue | None = None) -> None:
        self.catalogue = catalogue
        _PROCESSORS.append(self)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        attrs = span.attributes or {}
        iid = _interaction_id.get()
        if iid is not None and A_INTERACTION not in attrs:
            span.set_attribute(A_INTERACTION, iid)
        if self.catalogue is None or attrs.get("gen_ai.operation.name") != "execute_tool":
            return
        name = attrs.get("gen_ai.tool.name")
        entry = self.catalogue.get(name) if isinstance(name, str) else None
        if entry is None:
            return
        if A_EXTERNAL not in attrs:
            span.set_attribute(A_EXTERNAL, entry.is_external)
        if entry.is_mutating is not None and A_MUTATING not in attrs:
            span.set_attribute(A_MUTATING, entry.is_mutating)

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# ── tool spans for calls that never ran ──────────────────────────────────────────────────────

# Tools whose invocation an integration wraps, so it knows which of their spans the tool ran in;
# and those spans, by (trace_id, span_id), until the marker sees them. Bounded: a span that is never
# exported (no exporter, or dropped by its queue) must not grow this forever.
_WATCHED_TOOLS: set[str] = set()
_RAN: OrderedDict[tuple[int, int], None] = OrderedDict()
_RAN_MAX = 10_000


def watch_tool(name: str) -> None:
    """``name``'s invocation is wrapped: its instrumentation's TOOL spans are marked
    ``valp.tool_ran = false`` unless ``tool_ran`` was called inside them."""
    _WATCHED_TOOLS.add(name)


def tool_ran(span: Any) -> None:
    """The watched tool's body is running inside ``span``."""
    ctx = span.get_span_context()
    _RAN[(ctx.trace_id, ctx.span_id)] = None
    while len(_RAN) > _RAN_MAX:
        _RAN.popitem(last=False)


class ValpToolRanMarker(SpanExporter):
    """Marks ``valp.tool_ran = false`` on the TOOL span an instrumentation opened for a watched
    tool that then never ran, and exports every span.

    The OpenAI Agents SDK opens a tool's span BEFORE it checks whether the call needs approval, so
    a call paused for approval, or denied, leaves a TOOL span in which nothing ran. Unmarked, VALP
    reads it as a tool call of unknown authority, one for a refund a person refused. Marked, each
    consumer decides: VALP stores it and reads it as OTHER (it is not a tool call, and the approval
    request and decision are already their own spans), while the normaliser keeps it, because a
    denied call is a control that worked.

    Spans of tools no integration watches, and spans in which the tool ran, pass through unchanged.
    """

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([_with_attributes(s, {A_TOOL_RAN: False}) if _never_ran(s) else s
                                  for s in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def _never_ran(span: ReadableSpan) -> bool:
    attrs = span.attributes or {}
    if attrs.get("openinference.span.kind") != "TOOL" or attrs.get("tool.name") not in _WATCHED_TOOLS:
        return False
    ctx = span.get_span_context()
    key = (ctx.trace_id, ctx.span_id)
    if key in _RAN:
        del _RAN[key]
        return False
    return True


# ── gap detection: a sequence number per emitter, stamped at export ──────────────────────────


class ValpSequencingExporter(SpanExporter):
    """Wraps the OTLP exporter and numbers every span it exports: ``valp.emitter_id`` (a ULID for
    this process) and ``valp.span_seq`` (0, 1, 2, … with no gaps).

    Numbering at export rather than at span start means the sequence follows the order spans leave
    the process, so the VALP sink can treat a missing number as a real loss downstream of the SDK.
    A restart is a new emitter starting again at 0, so nothing needs to persist. A batch the inner
    exporter fails to deliver keeps its numbers, and so shows as a gap: that is a loss too.

    A span marked ``valp.tool_ran = false`` gets no ``span_seq``: it is not one of the events VALP
    counts, and the numbering stays the one VALP's coverage has always read (VALP's collector
    pipeline once dropped these spans). It still carries ``emitter_id``.
    """

    def __init__(self, inner: SpanExporter, emitter_id: str | None = None) -> None:
        self._inner = inner
        self.emitter_id = emitter_id or new_ulid()
        self._next = 0
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        counted = [(s.attributes or {}).get(A_TOOL_RAN) is not False for s in spans]
        with self._lock:
            seq, self._next = self._next, self._next + sum(counted)
        numbered = []
        for s, c in zip(spans, counted):
            extra: dict[str, Any] = {A_EMITTER: self.emitter_id}
            if c:
                extra[A_SPAN_SEQ], seq = seq, seq + 1
            numbered.append(_with_attributes(s, extra))
        return self._inner.export(numbered)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _with_attributes(span: ReadableSpan, extra: dict[str, Any]) -> ReadableSpan:
    """A copy of an ended span with attributes added (an ended span's own are read-only)."""
    return ReadableSpan(
        name=span.name, context=span.context, parent=span.parent, resource=span.resource,
        attributes={**dict(span.attributes or {}), **extra}, events=span.events, links=span.links,
        kind=span.kind, status=span.status, start_time=span.start_time, end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


# ── helpers for the call sites ───────────────────────────────────────────────────────────────


def _tracer():
    return trace.get_tracer(_TRACER_NAME)


def _check(value: str | None, allowed: frozenset[str], what: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"{what} must be one of {sorted(allowed)} or None, not {value!r}")


def current_catalogue() -> ToolCatalogue | None:
    """The confirmed catalogue of the installed ``ValpSpanProcessor``, for integrations that set
    ``is_external`` / ``is_mutating`` on a span another instrumentation created."""
    return next((p.catalogue for p in reversed(_PROCESSORS) if p.catalogue is not None), None)


def current_interaction_id() -> str | None:
    return _interaction_id.get()


class _Interaction:
    def __init__(self, span: Any, interaction_id: str) -> None:
        self.span, self.interaction_id = span, interaction_id
        self.disposition: str | None = None

    def set_disposition(self, disposition: str) -> None:
        _check(disposition, DISPOSITIONS, "disposition")
        self.disposition = disposition


@contextlib.contextmanager
def bind_interaction(interaction_id: str) -> Iterator[None]:
    """Stamp ``interaction_id`` on every span started inside, without opening an interaction span.

    For integrations whose interaction outlives one call (a run paused for human approval and
    resumed later): they bind the id around each part, and emit the interaction's single
    ``invoke_agent`` span with ``end_interaction`` once it has really ended.
    """
    token = _interaction_id.set(interaction_id)
    try:
        yield
    finally:
        _interaction_id.reset(token)


def end_interaction(name: str, interaction_id: str, start_time_ns: int, disposition: str | None, *,
                    previous_interaction_id: str | None = None) -> None:
    """Emit an interaction's ``invoke_agent`` span, from its recorded start to now.

    ``previous_interaction_id`` marks an interaction that continues one whose pause expired, so the
    join between the two is visible in the evidence.
    """
    _check(disposition, DISPOSITIONS, "disposition")
    attrs: dict[str, Any] = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name,
                             A_INTERACTION: interaction_id}
    if disposition is not None:
        attrs[A_DISPOSITION] = disposition
    if previous_interaction_id is not None:
        attrs[A_PREVIOUS_INTERACTION] = previous_interaction_id
    _tracer().start_span(f"invoke_agent {name}", attributes=attrs, start_time=start_time_ns).end()


@contextlib.contextmanager
def interaction(name: str = "agent", *, interaction_id: str | None = None) -> Iterator[_Interaction]:
    """One interaction: an ``invoke_agent`` span, and a ``valp.interaction_id`` on every span inside.

    Call ``set_disposition`` to record how it ended. An exception leaving the block records
    ``failed``; otherwise the disposition is omitted unless set, because a normal return does not
    by itself say whether the agent completed the task or abandoned it.
    """
    iid = interaction_id or new_ulid()
    token = _interaction_id.set(iid)
    try:
        with _tracer().start_as_current_span(
            f"invoke_agent {name}",
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name, A_INTERACTION: iid},
        ) as span:
            handle = _Interaction(span, iid)
            try:
                yield handle
            except BaseException:
                handle.disposition = "failed"
                raise
            finally:
                if handle.disposition is not None:
                    span.set_attribute(A_DISPOSITION, handle.disposition)
    finally:
        _interaction_id.reset(token)


def _tool_attrs(name: str, authority: str | None, is_mutating: bool | None,
                is_external: bool | None) -> dict[str, Any]:
    _check(authority, AUTHORITIES, "authority")
    attrs: dict[str, Any] = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}
    if authority is not None:
        attrs[A_AUTHORITY] = authority
    if is_mutating is not None:
        attrs[A_MUTATING] = bool(is_mutating)
    if is_external is not None:
        attrs[A_EXTERNAL] = bool(is_external)
    return attrs


@contextlib.contextmanager
def tool_call(name: str, *, authority: str | None = None, is_mutating: bool | None = None,
              is_external: bool | None = None) -> Iterator[Any]:
    """Wrap one tool invocation.

    If the current span is already this tool's ``execute_tool`` span (an instrumentation made
    it), the values are set on it; otherwise an ``execute_tool`` span is opened. ``authority`` is
    the call's supervision, known only here: pass it, or it is omitted. ``is_mutating`` is for a
    per-call tool, where the call site knows whether this call changed anything.
    """
    attrs = _tool_attrs(name, authority, is_mutating, is_external)
    current = trace.get_current_span()
    cur_attrs = getattr(current, "attributes", None) or {}
    if (current.is_recording() and cur_attrs.get("gen_ai.operation.name") == "execute_tool"
            and cur_attrs.get("gen_ai.tool.name") == name):
        for k, v in attrs.items():
            current.set_attribute(k, v)
        yield current
        return
    with _tracer().start_as_current_span(f"execute_tool {name}", attributes=attrs) as span:
        yield span


def _event_span(name: str, operation: str, attributes: dict[str, Any]) -> None:
    span = _tracer().start_span(name, attributes={A_OPERATION: operation, **attributes})
    span.end()


def approval_request(tool_name: str | None = None) -> None:
    """Record that a human's approval was asked for (a ``HUMAN_APPROVAL_REQUEST``)."""
    _event_span("valp.human_approval_request", "human_approval_request",
                {"gen_ai.tool.name": tool_name} if tool_name else {})


def approval_decision(outcome: str, tool_name: str | None = None, *, scope: str | None = None) -> None:
    """Record a human's decision (a ``HUMAN_APPROVAL_DECISION``): granted, denied or timeout.

    ``scope`` is ``call`` (this call only) or ``standing`` (this and every later call to the
    tool); omitted when not known.
    """
    if outcome is None:
        raise ValueError("an approval decision needs its outcome")
    _check(outcome, APPROVAL_OUTCOMES, "approval outcome")
    _check(scope, APPROVAL_SCOPES, "approval scope")
    attrs: dict[str, Any] = {A_OUTCOME: outcome}
    if scope is not None:
        attrs[A_APPROVAL_SCOPE] = scope
    if tool_name:
        attrs["gen_ai.tool.name"] = tool_name
    _event_span("valp.human_approval_decision", "human_approval_decision", attrs)


def guardrail_trip(action: str) -> None:
    """Record that a guardrail fired (a ``GUARDRAIL_TRIP``): it blocked, modified or warned.

    The guardrail's name is not recorded: it is free text the insured chose, and VALP rates on the
    action alone.
    """
    if action is None:
        raise ValueError("a guardrail trip needs its action")
    _check(action, GUARDRAIL_ACTIONS, "guardrail action")
    _event_span("valp.guardrail_trip", "guardrail_trip", {A_GUARDRAIL: action})


def set_context_class(context_class: str, span: Any | None = None) -> None:
    """Set ``valp.context_class`` on an inference span (the current one by default).

    The class comes from a taxonomy fixed at binding, assigned by a classifier running inside the
    insured's environment. ASCII, at most 32 characters.
    """
    if not (isinstance(context_class, str) and 0 < len(context_class) <= 32
            and all(0x20 < ord(c) < 0x7F for c in context_class)):
        raise ValueError("context_class must be 1-32 printable ASCII characters with no spaces")
    (span or trace.get_current_span()).set_attribute(A_CONTEXT_CLASS, context_class)
