"""OpenAI Agents SDK integration for the ``valp.*`` attributes.

The two integration points VALP needs, for this framework:

- **Tool dispatch.** ``run`` wraps each ``FunctionTool``'s ``on_invoke_tool``, so the tool call's
  span covers the tool body itself: it is the current span while the tool runs (spans the tool
  creates nest under it), and it is ended, with status ERROR and ``error.type`` set to the
  exception's class, however the tool exits. Its ``valp.authority``:

  - ``human_approved`` — a person approved THIS call (recorded by ``approve`` below);
  - ``human_preapproved`` — the call ran under a standing approval (``always_approve=True`` on an
    earlier call), or under an approval this integration did not see being given
    (``state.approve`` called directly): we cannot claim a per-call review we did not observe;
  - ``autonomous`` — the tool needed no approval. The SDK only runs a tool that needed none or
    was approved, so this is exact, not a default.

  If OpenInference's Agents SDK instrumentation is installed (the ``frameworks`` extra), its TOOL
  span is already the current span inside ``on_invoke_tool``: the ``valp.*`` attributes go on it,
  and no second span is opened, so there is exactly one tool span per call either way. It stays
  installed because it is also what records the model calls (the Agents SDK uses the Responses
  API, which the OpenAI instrumentation does not cover). ``is_external`` / ``is_mutating`` come
  from the confirmed catalogue.

  The Agents SDK opens that TOOL span BEFORE it checks whether the call needs approval, so a call
  paused for approval, or denied, leaves one in which the tool never ran. The wrapper records the
  spans the tool did run in (``valp.tool_ran()``), and ``init()``'s exporter marks the others
  ``valp.tool_ran = false`` (``valp.ValpToolRanMarker``): VALP stores them and never reads them as
  tool calls, and the normaliser keeps them.

- **The approval primitive** (``needs_approval`` → interruption → ``RunState.approve/reject``).
  ``run`` records a ``HUMAN_APPROVAL_REQUEST`` for each pending approval; ``approve``, ``reject``
  and ``expire`` record the ``HUMAN_APPROVAL_DECISION`` (granted, denied, timeout), with
  ``valp.approval_scope`` ``call`` or ``standing``, and then apply it.

One interaction is one ``run`` call, including every resumption after an approval: the
``valp.interaction_id`` is carried across the pause, and the single ``invoke_agent`` span is
emitted when the run finally ends: ``completed`` when it produced a final output, ``failed`` when it
raised, or when a pending approval expired (below). A run that returns paused for approval emits no
``invoke_agent`` span yet; the interaction is still open.

**Known limitation: resuming in another process.** The pause is remembered in this process's
memory (``_pending``, ``_by_state``). A caller that serialises the ``RunState`` (``to_json``) and
resumes it in a different process — the normal case when a person takes hours to decide — gets a
NEW interaction id on resume, so the interaction counts twice, and decision spans recorded there
carry no interaction id. The fix is to carry ``(interaction_id, start_ns)`` on something that
survives the pause, such as the run context the caller already serialises; not done yet.

Usage::

    from redberry_sdk.integrations import openai_agents as valp_agents

    result = await valp_agents.run(agent, "refund order 1234")
    while result.interruptions:
        state = result.to_state()
        for item in result.interruptions:
            if ask_a_person(item):
                valp_agents.approve(state, item)
            else:
                valp_agents.reject(state, item)
        result = await valp_agents.run(agent, state)

The Agents SDK is imported lazily: this module imports without it, and fails only when used.
"""

from __future__ import annotations

import contextvars
import os
import time
import types
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from redberry_sdk import valp

__all__ = ["ValpRunHooks", "run", "approve", "reject", "expire", "sweep"]


@dataclass
class _Pending:
    """One approval a paused run is waiting on."""

    iid: str          # the interaction it belongs to
    start_ns: int     # when that interaction started
    name: str         # the agent's name, for its invoke_agent span
    tool: str | None
    paused_ns: int    # when the run paused for it
    decided: bool = False


# call_id of a pending approval -> its _Pending. Filled when a run pauses, read when the decision is
# recorded and when the run resumes.
_pending: dict[str, _Pending] = {}
# RunState objects a decision was applied to -> the interaction, so resuming with that state carries
# it on even though its interruptions are now cleared. Keyed by id() with a weak reference to
# confirm identity (RunState is not hashable).
_by_state: dict[int, tuple[weakref.ref, str]] = {}
# Call ids a person approved individually: their tool span is human_approved, not preapproved.
_approved_calls: set[str] = set()
# Interactions closed by the TTL -> kept briefly so a late resume can say which one it follows.
_expired: OrderedDict[str, str] = OrderedDict()  # call_id or "state:<id>" -> expired interaction id
_EXPIRED_KEEP = 10_000

# Set for the duration of run(): the tool wrappers act only then, so a tool shared with code that
# does not use this integration behaves exactly as before.
_active: contextvars.ContextVar[bool] = contextvars.ContextVar("valp_agents_active", default=False)

# ── pending-approval TTL ─────────────────────────────────────────────────────────────────────
# A run that pauses for approval and is never resumed (nobody answers, or the caller drops the
# state) would otherwise stay in _pending for the life of the process and never emit its
# invoke_agent span, so VALP would never see the interaction end, and a long-lived worker would
# leak. So pending entries expire.
#
# On expiry we record what actually happened — nobody decided in time: a `timeout` decision — and
# close the interaction as `failed`. An entry that WAS decided but never resumed is closed without a
# second decision record (the evidence must not show a grant followed by a timeout). All entries of
# one interaction expire together: they share one pause. This is a recorded outcome, never a
# silent drop.
#
# Default 7 days (SDKConfig.valp_pending_approval_ttl_s / VALP_PENDING_APPROVAL_TTL_S): long enough
# for a human approval queue, short enough that abandoned runs don't accumulate. Raise it for
# workflows whose approvals legitimately take longer.
#
# NB: a spike of `failed` interactions whose duration is ~exactly the TTL means THIS fired, not that
# the agent failed. Swept lazily on every run()/approve()/reject()/expire() and at shutdown().
# A run resumed after its interaction expired starts a NEW interaction, marked with
# valp.previous_interaction_id so the join is visible.
_DEFAULT_PENDING_TTL_S = 7 * 24 * 3600


def _pending_ttl_s() -> float:
    env = os.environ.get("VALP_PENDING_APPROVAL_TTL_S", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    configured = valp.settings().get("pending_approval_ttl_s")
    return float(configured) if configured is not None else float(_DEFAULT_PENDING_TTL_S)


def _forget_expired(key: str, iid: str) -> None:
    _expired[key] = iid
    while len(_expired) > _EXPIRED_KEEP:
        _expired.popitem(last=False)


def sweep(now_ns: int | None = None) -> None:
    """Close every pending interaction whose pause is older than the TTL (see the comment above)."""
    now_ns = now_ns if now_ns is not None else time.time_ns()
    ttl_ns = _pending_ttl_s() * 1_000_000_000
    expired_iids = {p.iid for p in _pending.values() if now_ns - p.paused_ns > ttl_ns}
    for iid in expired_iids:
        entries = [(cid, p) for cid, p in _pending.items() if p.iid == iid]
        with valp.bind_interaction(iid):
            for _cid, p in entries:
                if not p.decided:
                    valp.approval_decision("timeout", p.tool)
        first = entries[0][1]
        valp.end_interaction(first.name, iid, first.start_ns, "failed")
        for cid, _p in entries:
            del _pending[cid]
            _approved_calls.discard(cid)
            _forget_expired(cid, iid)
        for key, (_ref, state_iid) in list(_by_state.items()):
            if state_iid == iid:
                del _by_state[key]
                _forget_expired(f"state:{key}", iid)


# ── state across the pause ───────────────────────────────────────────────────────────────────


def _remember(state: Any, iid: str) -> None:
    try:
        ref = weakref.ref(state, lambda _r, k=id(state): _by_state.pop(k, None))
    except TypeError:
        return
    _by_state[id(state)] = (ref, iid)


def _recall(state: Any) -> str | None:
    entry = _by_state.get(id(state))
    return entry[1] if entry is not None and entry[0]() is state else None


def _call_id(item: Any) -> str | None:
    raw = getattr(item, "raw_item", None)
    cid = raw.get("call_id") if isinstance(raw, dict) else getattr(raw, "call_id", None)
    return cid if isinstance(cid, str) and cid else None


def _pending_for(iid: str) -> _Pending | None:
    return next((p for p in _pending.values() if p.iid == iid), None)


def _resumed(input: Any) -> tuple[_Pending | None, str | None]:
    """``(the paused interaction this input resumes, or None; the expired one it follows, or None)``."""
    iid = _recall(input)
    if iid is not None and (p := _pending_for(iid)) is not None:
        return p, None
    items = list(getattr(input, "get_interruptions", lambda: ())() or ())
    for item in items:
        cid = _call_id(item)
        if cid in _pending:
            return _pending[cid], None
    previous = _expired.get(f"state:{id(input)}") if iid is None else None
    for item in items:
        previous = previous or _expired.get(_call_id(item) or "")
    return None, previous


# ── tool dispatch ────────────────────────────────────────────────────────────────────────────


def _authority(context: Any, tool_name: str) -> str:
    call_id = getattr(context, "tool_call_id", None)
    if call_id and call_id in _approved_calls:
        return "human_approved"
    try:
        approved = context.is_tool_approved(tool_name, call_id) if call_id else None
    except Exception:  # noqa: BLE001 — an SDK without per-call approvals: nothing was approved
        approved = None
    return "human_preapproved" if approved is True else "autonomous"


def _tool_attributes(context: Any, tool_name: str) -> dict[str, Any]:
    attrs: dict[str, Any] = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool_name,
                             valp.A_AUTHORITY: _authority(context, tool_name)}
    catalogue = valp.current_catalogue()
    entry = catalogue.get(tool_name) if catalogue is not None else None
    if entry is not None:
        attrs[valp.A_EXTERNAL] = entry.is_external
        if entry.is_mutating is not None:
            attrs[valp.A_MUTATING] = entry.is_mutating
    return attrs


def _is_openinference_tool_span(span: Any, tool_name: str) -> bool:
    attrs = getattr(span, "attributes", None) or {}
    return (span.is_recording() and attrs.get("openinference.span.kind") == "TOOL"
            and attrs.get("tool.name", tool_name) == tool_name and getattr(span, "name", "") == tool_name)


def _fail(span: Any, exc: BaseException) -> None:
    span.set_attribute("error.type", type(exc).__name__)  # the class only: a message can quote content
    span.set_status(Status(StatusCode.ERROR))


def _wrap_tool(tool: Any) -> None:
    """Make ``tool``'s invocation carry the tool call's span. Idempotent."""
    if getattr(tool, "_valp_wrapped", False) or not callable(getattr(tool, "on_invoke_tool", None)):
        return
    original = tool.on_invoke_tool
    name = tool.name
    valp.watch_tool(name)

    async def on_invoke_tool(context: Any, arguments: str) -> Any:
        current = trace.get_current_span()
        is_oi_span = _is_openinference_tool_span(current, name)
        if is_oi_span:
            valp.tool_ran(current)  # the span a paused or denied call leaves never gets here
        if not _active.get():
            return await original(context, arguments)
        attrs = _tool_attributes(context, name)
        if is_oi_span:
            # OpenInference's own TOOL span for this call: annotate it, open no second one.
            for k, v in attrs.items():
                current.set_attribute(k, v)
            try:
                return await original(context, arguments)
            except BaseException as exc:
                _fail(current, exc)
                raise
        with trace.get_tracer("redberry.valp").start_as_current_span(
                f"execute_tool {name}", attributes=attrs, record_exception=False,
                set_status_on_exception=False) as span:
            try:
                return await original(context, arguments)
            except BaseException as exc:
                _fail(span, exc)
                raise

    tool.on_invoke_tool = on_invoke_tool
    tool._valp_wrapped = True


def _wrap_agent_tools(agent: Any, seen: set[int] | None = None) -> None:
    """Wrap the tools of ``agent`` and of every agent it can hand off to."""
    seen = set() if seen is None else seen
    if agent is None or id(agent) in seen:
        return
    seen.add(id(agent))
    for tool in getattr(agent, "tools", None) or ():
        _wrap_tool(tool)
    for h in getattr(agent, "handoffs", None) or ():
        _wrap_agent_tools(h if hasattr(h, "tools") else getattr(h, "agent", None), seen)


def _hooks_base():
    from agents import RunHooks
    return RunHooks


class _HooksMixin:
    """Spans for tools the wrapper cannot reach (anything that is not a ``FunctionTool``), and
    delegation of every hook to the caller's own hooks if given."""

    def __init__(self, inner: Any | None = None) -> None:
        self._inner = inner
        self._spans: dict[Any, Any] = {}

    async def _delegate(self, name: str, *args: Any) -> None:
        if self._inner is not None:
            await getattr(self._inner, name)(*args)

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        name = getattr(tool, "name", None)
        if isinstance(name, str) and name and not getattr(tool, "_valp_wrapped", False):
            span = trace.get_tracer("redberry.valp").start_span(f"execute_tool {name}",
                                                               attributes=_tool_attributes(context, name))
            self._spans[getattr(context, "tool_call_id", None) or id(context)] = span
        await self._delegate("on_tool_start", context, agent, tool)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        span = self._spans.pop(getattr(context, "tool_call_id", None) or id(context), None)
        if span is not None:
            span.end()
        await self._delegate("on_tool_end", context, agent, tool, result)

    def end_open_spans(self, exc: BaseException) -> None:
        """A run that raised: end every tool span still open, as failed, so none is lost."""
        for span in self._spans.values():
            _fail(span, exc)
            span.end()
        self._spans.clear()

    async def on_agent_start(self, *a: Any) -> None:
        await self._delegate("on_agent_start", *a)

    async def on_agent_end(self, *a: Any) -> None:
        await self._delegate("on_agent_end", *a)

    async def on_handoff(self, *a: Any) -> None:
        await self._delegate("on_handoff", *a)

    async def on_llm_start(self, *a: Any) -> None:
        await self._delegate("on_llm_start", *a)

    async def on_llm_end(self, *a: Any) -> None:
        await self._delegate("on_llm_end", *a)


_hooks_cls: type | None = None


def ValpRunHooks(inner: Any | None = None) -> Any:  # noqa: N802 — a class, built lazily
    """``RunHooks`` for tools the wrapper cannot reach; ``inner`` is the caller's own hooks, if any."""
    global _hooks_cls
    if _hooks_cls is None:  # RunHooks is a generic alias, so the class is built with new_class
        _hooks_cls = types.new_class("ValpRunHooks", (_HooksMixin, _hooks_base()))
    return _hooks_cls(inner)


# ── run, and the decisions ───────────────────────────────────────────────────────────────────


async def run(starting_agent: Any, input: Any, *, hooks: Any | None = None, **kwargs: Any) -> Any:
    """``Runner.run`` with the ``valp.*`` instrumentation; same arguments, same result."""
    from agents import Runner

    sweep()
    carried, previous = _resumed(input)
    if carried is not None:
        iid, start_ns, name = carried.iid, carried.start_ns, carried.name
        for cid in [c for c, p in _pending.items() if p.iid == iid]:
            del _pending[cid]
    else:
        iid, start_ns, name = valp.new_ulid(), time.time_ns(), getattr(starting_agent, "name", "agent")

    _wrap_agent_tools(starting_agent)
    run_hooks = ValpRunHooks(hooks)
    token = _active.set(True)
    try:
        with valp.bind_interaction(iid):
            try:
                result = await Runner.run(starting_agent, input, hooks=run_hooks, **kwargs)
            except BaseException as exc:
                run_hooks.end_open_spans(exc)
                valp.end_interaction(name, iid, start_ns, "failed", previous_interaction_id=previous)
                raise
            items = list(getattr(result, "interruptions", None) or ())
            if items:
                now = time.time_ns()
                for item in items:
                    cid = _call_id(item)
                    tool = getattr(item, "tool_name", None)
                    if cid is not None:
                        _pending[cid] = _Pending(iid, start_ns, name, tool, now)
                    valp.approval_request(tool)
            else:
                valp.end_interaction(name, iid, start_ns, "completed", previous_interaction_id=previous)
    finally:
        _active.reset(token)
    return result


def _decide(state: Any, item: Any, outcome: str, scope: str) -> None:
    sweep()
    pending = _pending.get(_call_id(item) or "")
    tool = getattr(item, "tool_name", None)
    if pending is None:
        valp.approval_decision(outcome, tool, scope=scope)
        return
    pending.decided = True
    _remember(state, pending.iid)
    with valp.bind_interaction(pending.iid):
        valp.approval_decision(outcome, tool, scope=scope)


def approve(state: Any, item: Any, *, always_approve: bool = False) -> None:
    """A person granted the approval: record it, then ``state.approve``.

    The call they looked at is ``human_approved``. With ``always_approve=True`` the decision is
    recorded as a STANDING one, and later calls to the tool run as ``human_preapproved``: a person
    approved that kind of action, not each call.
    """
    _decide(state, item, "granted", "standing" if always_approve else "call")
    cid = _call_id(item)
    if cid:
        _approved_calls.add(cid)
    state.approve(item, always_approve=always_approve)


def reject(state: Any, item: Any, *, always_reject: bool = False, rejection_message: str | None = None) -> None:
    """A person denied the approval: record it (standing with ``always_reject=True``), then
    ``state.reject``."""
    _decide(state, item, "denied", "standing" if always_reject else "call")
    state.reject(item, always_reject=always_reject, rejection_message=rejection_message)


def expire(state: Any, item: Any, *, rejection_message: str | None = None) -> None:
    """Nobody decided in time: record ``timeout``, then reject the call so the run can continue."""
    _decide(state, item, "timeout", "call")
    state.reject(item, rejection_message=rejection_message)
