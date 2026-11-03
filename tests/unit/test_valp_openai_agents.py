"""The OpenAI Agents SDK integration: real Runner runs, driven by the SDK's own scripted model."""

from __future__ import annotations

import time

import pytest

agents = pytest.importorskip("agents")
from agents import Agent, function_tool, set_tracing_disabled  # noqa: E402
from agents.testing import ScriptedModel, assistant_message, function_call  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from redberry_sdk import valp  # noqa: E402
from redberry_sdk.integrations import openai_agents as valp_agents  # noqa: E402

from .test_valp_attributes import _EXPORTER, spans  # noqa: E402,F401 — shared provider and fixture

set_tracing_disabled(True)
TTL_NS = 7 * 24 * 3600 * 1_000_000_000


@pytest.fixture(autouse=True)
def clean_integration_state():
    yield
    valp_agents._pending.clear()
    valp_agents._by_state.clear()
    valp_agents._approved_calls.clear()
    valp_agents._expired.clear()


@function_tool
def lookup_order(order_id: str) -> str:
    """Look an order up."""
    with trace.get_tracer("tool-internals").start_as_current_span("db.query"):
        return f"order {order_id}: 48.00"


@function_tool(needs_approval=True)
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount} on {order_id}"


@function_tool(failure_error_function=None)
def flaky(order_id: str) -> str:
    """Always fails."""
    raise TimeoutError("Amara Okafor's order 1234 timed out")


def _agent(*steps, tools=(lookup_order, issue_refund)) -> Agent:
    return Agent(name="support", instructions="help", tools=list(tools), model=ScriptedModel(list(steps)))


def _finished() -> list[tuple[str, dict]]:
    return [(s.name, dict(s.attributes or {})) for s in _EXPORTER.get_finished_spans()]


def _named(prefix: str) -> list[dict]:
    return [a for n, a in _finished() if n.startswith(prefix)]


async def _pause(call_id: str = "c2") -> tuple[Agent, object, object]:
    agent = _agent([function_call("issue_refund", {"order_id": "1234", "amount": 48.0}, call_id=call_id)],
                   [assistant_message("done")])
    first = await valp_agents.run(agent, "refund 1234")
    assert first.interruptions
    return agent, first, first.to_state()


# ── tool dispatch ──────────────────────────────────────────────────────────────────────────


async def test_autonomous_tool_call(spans) -> None:
    agent = _agent([function_call("lookup_order", {"order_id": "1234"}, call_id="c1")],
                   [assistant_message("48.00")])
    result = await valp_agents.run(agent, "what did 1234 cost?")
    assert result.final_output == "48.00"
    (tool,) = _named("execute_tool lookup_order")
    assert tool[valp.A_AUTHORITY] == "autonomous"
    assert (tool[valp.A_EXTERNAL], tool[valp.A_MUTATING]) == (False, False)  # from the catalogue
    (end,) = _named("invoke_agent support")
    assert end[valp.A_DISPOSITION] == "completed" and tool[valp.A_INTERACTION] == end[valp.A_INTERACTION]


async def test_the_tool_span_is_current_while_the_tool_runs(spans) -> None:
    agent = _agent([function_call("lookup_order", {"order_id": "1"}, call_id="c1")], [assistant_message("ok")])
    await valp_agents.run(agent, "x")
    by_name = {s.name: s for s in _EXPORTER.get_finished_spans()}
    assert by_name["db.query"].parent.span_id == by_name["execute_tool lookup_order"].context.span_id


async def test_a_raising_tool_still_has_its_span_marked_failed(spans) -> None:
    agent = _agent([function_call("flaky", {"order_id": "1234"}, call_id="c9")], [assistant_message("?")],
                   tools=(flaky,))
    with pytest.raises(Exception):  # noqa: B017 — the SDK wraps the tool's exception in its own
        await valp_agents.run(agent, "go")
    (tool,) = _named("execute_tool flaky")
    span = next(s for s in _EXPORTER.get_finished_spans() if s.name == "execute_tool flaky")
    assert span.status.status_code == trace.StatusCode.ERROR and tool["error.type"] == "TimeoutError"
    assert "Amara" not in repr(span.status) and not span.events  # the class, never the message
    assert _named("invoke_agent support")[0][valp.A_DISPOSITION] == "failed"


async def test_tools_used_outside_the_integration_are_untouched(spans) -> None:
    from agents import Runner
    agent = _agent([function_call("lookup_order", {"order_id": "1"}, call_id="c1")], [assistant_message("ok")])
    await valp_agents.run(agent, "x")  # wraps lookup_order
    _EXPORTER.clear()
    agent2 = _agent([function_call("lookup_order", {"order_id": "2"}, call_id="c2")], [assistant_message("ok")])
    await Runner.run(agent2, "y")
    assert not _named("execute_tool")


async def test_callers_own_hooks_still_run(spans) -> None:
    seen = []

    class Mine(agents.RunHooks):
        async def on_tool_start(self, context, agent, tool):
            seen.append(tool.name)

    agent = _agent([function_call("lookup_order", {"order_id": "1"}, call_id="c4")], [assistant_message("ok")])
    await valp_agents.run(agent, "x", hooks=Mine())
    assert seen == ["lookup_order"]


# ── approvals ──────────────────────────────────────────────────────────────────────────────


async def test_approved_call_across_a_pause_is_one_interaction(spans) -> None:
    agent, first, state = await _pause()
    assert not _named("invoke_agent")  # still open
    for item in first.interruptions:
        valp_agents.approve(state, item)
    second = await valp_agents.run(agent, state)
    assert second.final_output == "done"
    (end,) = _named("invoke_agent support")
    iid = end[valp.A_INTERACTION]
    assert _named("valp.human_approval_request")[0][valp.A_INTERACTION] == iid
    (dec,) = _named("valp.human_approval_decision")
    assert (dec[valp.A_OUTCOME], dec[valp.A_APPROVAL_SCOPE], dec[valp.A_INTERACTION]) == ("granted", "call", iid)
    (tool,) = _named("execute_tool issue_refund")
    assert (tool[valp.A_AUTHORITY], tool[valp.A_INTERACTION]) == ("human_approved", iid)
    assert (tool[valp.A_EXTERNAL], tool[valp.A_MUTATING]) == (True, True)


async def test_a_standing_approval_is_not_a_per_call_review(spans) -> None:
    """approve(always_approve=True), then the tool runs three times: the call the person looked at
    is human_approved; the two later ones ran under the standing approval."""
    agent = _agent([function_call("issue_refund", {"order_id": "1", "amount": 5.0}, call_id="s1")],
                   [function_call("issue_refund", {"order_id": "2", "amount": 5.0}, call_id="s2"),
                    function_call("issue_refund", {"order_id": "3", "amount": 5.0}, call_id="s3")],
                   [assistant_message("done")])
    first = await valp_agents.run(agent, "refund three")
    state = first.to_state()
    for item in first.interruptions:
        valp_agents.approve(state, item, always_approve=True)
    result = await valp_agents.run(agent, state)
    assert result.final_output == "done" and not result.interruptions
    authorities = sorted(a[valp.A_AUTHORITY] for a in _named("execute_tool issue_refund"))
    assert authorities == ["human_approved", "human_preapproved", "human_preapproved"]
    (dec,) = _named("valp.human_approval_decision")
    assert dec[valp.A_APPROVAL_SCOPE] == "standing"


async def test_an_approval_we_did_not_see_is_preapproved(spans) -> None:
    agent, first, state = await _pause()
    for item in first.interruptions:
        state.approve(item)  # directly, not through valp_agents.approve
    await valp_agents.run(agent, state)
    (tool,) = _named("execute_tool issue_refund")
    assert tool[valp.A_AUTHORITY] == "human_preapproved"
    assert not _named("valp.human_approval_decision")  # nothing is claimed about a decision we did not see


@pytest.mark.parametrize("decide, outcome", [(valp_agents.reject, "denied"), (valp_agents.expire, "timeout")])
async def test_denied_or_timed_out_call_never_runs(spans, decide, outcome) -> None:
    agent, first, state = await _pause("c3")
    for item in first.interruptions:
        decide(state, item)
    await valp_agents.run(agent, state)
    assert _named("valp.human_approval_decision")[0][valp.A_OUTCOME] == outcome
    assert not _named("execute_tool issue_refund")


async def test_standing_rejection_is_recorded(spans) -> None:
    agent, first, state = await _pause("c4")
    for item in first.interruptions:
        valp_agents.reject(state, item, always_reject=True)
    assert _named("valp.human_approval_decision")[0][valp.A_APPROVAL_SCOPE] == "standing"


# ── the pending-approval TTL ───────────────────────────────────────────────────────────────


async def test_an_undecided_pause_past_the_ttl_is_a_timeout_and_a_failed_interaction(spans) -> None:
    await _pause()
    valp_agents.sweep(now_ns=time.time_ns() + TTL_NS + 1)
    (dec,) = _named("valp.human_approval_decision")
    (end,) = _named("invoke_agent support")
    assert dec[valp.A_OUTCOME] == "timeout" and end[valp.A_DISPOSITION] == "failed"
    assert dec[valp.A_INTERACTION] == end[valp.A_INTERACTION]
    assert valp_agents._pending == {}


async def test_decided_but_never_resumed_closes_without_a_second_decision(spans) -> None:
    _agent_, first, state = await _pause()
    for item in first.interruptions:
        valp_agents.approve(state, item)
    valp_agents.sweep(now_ns=time.time_ns() + TTL_NS + 1)
    assert [d[valp.A_OUTCOME] for d in _named("valp.human_approval_decision")] == ["granted"]
    assert _named("invoke_agent support")[0][valp.A_DISPOSITION] == "failed"


async def test_not_yet_expired_is_left_alone(spans) -> None:
    await _pause()
    valp_agents.sweep(now_ns=time.time_ns() + TTL_NS // 2)
    assert valp_agents._pending and not _named("invoke_agent")


async def test_resuming_after_expiry_starts_a_new_marked_interaction(spans) -> None:
    agent, first, state = await _pause()
    for item in first.interruptions:
        valp_agents.approve(state, item)
    valp_agents.sweep(now_ns=time.time_ns() + TTL_NS + 1)
    (expired,) = _named("invoke_agent support")
    _EXPORTER.clear()
    await valp_agents.run(agent, state)
    (fresh,) = _named("invoke_agent support")
    assert fresh[valp.A_INTERACTION] != expired[valp.A_INTERACTION]
    assert fresh[valp.A_PREVIOUS_INTERACTION] == expired[valp.A_INTERACTION]


def test_ttl_is_configurable(monkeypatch) -> None:
    assert valp_agents._pending_ttl_s() == 7 * 24 * 3600
    valp.configure(pending_approval_ttl_s=60)
    try:
        assert valp_agents._pending_ttl_s() == 60
        monkeypatch.setenv("VALP_PENDING_APPROVAL_TTL_S", "5")
        assert valp_agents._pending_ttl_s() == 5
    finally:
        valp._SETTINGS.pop("pending_approval_ttl_s", None)


# ── with OpenInference's own Agents SDK instrumentation installed ──────────────────────────


async def test_one_tool_span_per_call_with_openinference_active(spans) -> None:
    """Its TOOL span is annotated, not duplicated: one span per tool call, carrying valp.*."""
    oi = pytest.importorskip("openinference.instrumentation.openai_agents")
    instrumentor = oi.OpenAIAgentsInstrumentor()
    instrumentor.instrument(tracer_provider=trace.get_tracer_provider())
    set_tracing_disabled(False)
    try:
        agent = _agent([function_call("lookup_order", {"order_id": "1"}, call_id="o1")], [assistant_message("ok")])
        await valp_agents.run(agent, "x")
    finally:
        set_tracing_disabled(True)
        instrumentor.uninstrument()
    tool_spans = [a for _n, a in _finished()
                  if a.get("openinference.span.kind") == "TOOL" or a.get("gen_ai.operation.name") == "execute_tool"]
    assert len(tool_spans) == 1, tool_spans
    (tool,) = tool_spans
    assert tool["openinference.span.kind"] == "TOOL" and tool[valp.A_AUTHORITY] == "autonomous"
    assert (tool[valp.A_EXTERNAL], tool[valp.A_MUTATING]) == (False, False)


# The export path init() builds: a span the SDK opened for a call that never ran is marked here.
_EXPORTED = InMemorySpanExporter()
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(valp.ValpToolRanMarker(_EXPORTED)))


@pytest.fixture
def openinference(spans):
    """OpenInference's Agents instrumentation on, the SDK's tracing on; yields the exported
    ``issue_refund`` / ``lookup_order`` TOOL spans' attributes, as the collector receives them."""
    oi = pytest.importorskip("openinference.instrumentation.openai_agents")
    instrumentor = oi.OpenAIAgentsInstrumentor()
    instrumentor.instrument(tracer_provider=trace.get_tracer_provider())
    set_tracing_disabled(False)
    _EXPORTED.clear()
    yield lambda name: [dict(s.attributes or {}) for s in _EXPORTED.get_finished_spans()
                        if (s.attributes or {}).get("openinference.span.kind") == "TOOL"
                        and (s.attributes or {}).get("tool.name") == name]
    set_tracing_disabled(True)
    instrumentor.uninstrument()


async def test_a_pause_for_approval_is_exported_marked_not_run(openinference) -> None:
    """The SDK opens the tool's span before it checks for approval, so a paused call leaves a TOOL
    span whose tool never ran. It is exported, marked; the call that ran after the approval is not."""
    agent, first, state = await _pause("p1")
    for item in first.interruptions:
        valp_agents.approve(state, item)
    await valp_agents.run(agent, state)
    paused, ran = sorted(openinference("issue_refund"), key=lambda a: valp.A_TOOL_RAN in a, reverse=True)
    assert paused[valp.A_TOOL_RAN] is False and valp.A_AUTHORITY not in paused
    assert valp.A_TOOL_RAN not in ran and ran[valp.A_AUTHORITY] == "human_approved"


@pytest.mark.parametrize("decide", [valp_agents.reject, valp_agents.expire])
async def test_a_denied_call_is_exported_marked_not_run(openinference, decide) -> None:
    """Kept for the normaliser (a denied call is a control that worked); marked, so VALP's
    collector pipeline can drop it."""
    agent, first, state = await _pause("p2")
    for item in first.interruptions:
        decide(state, item)
    await valp_agents.run(agent, state)
    (tool,) = openinference("issue_refund")
    assert tool[valp.A_TOOL_RAN] is False and valp.A_AUTHORITY not in tool
    assert [s for s in _EXPORTED.get_finished_spans() if s.name == "valp.human_approval_decision"]


async def test_a_tool_that_ran_outside_the_integration_is_not_marked(openinference) -> None:
    from agents import Runner
    await valp_agents.run(_agent([function_call("lookup_order", {"order_id": "1"}, call_id="p3")],
                                 [assistant_message("ok")]), "x")  # wraps lookup_order
    _EXPORTED.clear()
    await Runner.run(_agent([function_call("lookup_order", {"order_id": "2"}, call_id="p4")],
                            [assistant_message("ok")]), "y")
    (tool,) = openinference("lookup_order")
    assert valp.A_TOOL_RAN not in tool and valp.A_AUTHORITY not in tool


def test_the_marker_exports_every_span_and_marks_only_watched_tools_that_never_ran() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(valp.ValpToolRanMarker(inner)))
    tracer = provider.get_tracer("t")
    valp.watch_tool("marker_probe")
    tool = {"openinference.span.kind": "TOOL", "tool.name": "marker_probe"}
    with tracer.start_as_current_span("plain"):
        pass
    with tracer.start_as_current_span("marker_probe", attributes=tool):
        pass  # paused: the tool never ran
    with tracer.start_as_current_span("marker_probe", attributes=tool) as ran:
        valp.tool_ran(ran)
    with tracer.start_as_current_span("other", attributes={"openinference.span.kind": "TOOL", "tool.name": "unwatched"}):
        pass
    got = [(s.name, (s.attributes or {}).get(valp.A_TOOL_RAN)) for s in inner.get_finished_spans()]
    assert got == [("plain", None), ("marker_probe", False), ("marker_probe", None), ("other", None)]
