"""valp.* span attributes: every value emitted when known, every value omitted when not."""

from __future__ import annotations

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from redberry_sdk import valp

CATALOGUE = {"v": 1, "tools": [
    {"name": "issue_refund", "is_external": True, "is_mutating": True, "mutation": "fixed"},
    {"name": "lookup_order", "is_external": False, "is_mutating": False, "mutation": "fixed"},
    {"name": "update_ticket", "is_external": False, "mutation": "per_call"},
]}

_EXPORTER = InMemorySpanExporter()
_PROCESSOR = valp.ValpSpanProcessor()


def _install() -> None:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(_PROCESSOR)
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))


_install()


@pytest.fixture
def spans():
    _EXPORTER.clear()
    _PROCESSOR.catalogue = valp.ToolCatalogue.from_dict(CATALOGUE)
    yield lambda: {s.name: dict(s.attributes or {}) for s in _EXPORTER.get_finished_spans()}
    _PROCESSOR.catalogue = None


def test_ulid_is_canonical() -> None:
    u = valp.new_ulid()
    assert len(u) == 26 and u == u.upper() and set(u) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_interaction_stamps_every_span_inside(spans) -> None:
    tracer = trace.get_tracer("some.instrumentation")
    with valp.interaction("support") as ix:
        with tracer.start_as_current_span("chat gpt-4o", attributes={"gen_ai.operation.name": "chat"}):
            pass
        ix.set_disposition("completed")
    got = spans()
    assert got["chat gpt-4o"][valp.A_INTERACTION] == ix.interaction_id
    end = got["invoke_agent support"]
    assert end["gen_ai.operation.name"] == "invoke_agent"
    assert end[valp.A_INTERACTION] == ix.interaction_id and end[valp.A_DISPOSITION] == "completed"


def test_outside_an_interaction_no_id(spans) -> None:
    with trace.get_tracer("x").start_as_current_span("chat"):
        pass
    assert valp.A_INTERACTION not in spans()["chat"]


def test_interaction_disposition_omitted_unless_known(spans) -> None:
    with valp.interaction("a"):
        pass
    assert valp.A_DISPOSITION not in spans()["invoke_agent a"]


def test_interaction_failed_on_exception(spans) -> None:
    with pytest.raises(RuntimeError), valp.interaction("a"):
        raise RuntimeError("boom")
    assert spans()["invoke_agent a"][valp.A_DISPOSITION] == "failed"


def test_catalogue_fills_fixed_tools(spans) -> None:
    with valp.tool_call("issue_refund", authority="autonomous"):
        pass
    a = spans()["execute_tool issue_refund"]
    assert (a[valp.A_EXTERNAL], a[valp.A_MUTATING], a[valp.A_AUTHORITY]) == (True, True, "autonomous")


def test_per_call_tool_gets_is_mutating_only_from_the_call_site(spans) -> None:
    with valp.tool_call("update_ticket"):
        pass
    with valp.tool_call("update_ticket", is_mutating=True):
        pass
    first, second = [dict(s.attributes) for s in _EXPORTER.get_finished_spans()]
    assert first[valp.A_EXTERNAL] is False and valp.A_MUTATING not in first
    assert second[valp.A_MUTATING] is True


def test_unlisted_tool_and_no_catalogue_get_nothing(spans) -> None:
    with valp.tool_call("mystery"):
        pass
    _PROCESSOR.catalogue = None
    with valp.tool_call("issue_refund"):
        pass
    for s in _EXPORTER.get_finished_spans():
        for k in (valp.A_EXTERNAL, valp.A_MUTATING, valp.A_AUTHORITY):
            assert k not in s.attributes, (s.name, k)


def test_tool_call_annotates_an_existing_execute_tool_span(spans) -> None:
    tracer = trace.get_tracer("framework")
    with tracer.start_as_current_span("execute_tool lookup_order",
                                      attributes={"gen_ai.operation.name": "execute_tool",
                                                  "gen_ai.tool.name": "lookup_order"}):
        with valp.tool_call("lookup_order", authority="human_initiated"):
            pass
    got = spans()
    assert list(got) == ["execute_tool lookup_order"]
    assert got["execute_tool lookup_order"][valp.A_AUTHORITY] == "human_initiated"


def test_catalogue_never_overwrites_the_call_site(spans) -> None:
    with valp.tool_call("lookup_order", is_mutating=True):
        pass
    assert spans()["execute_tool lookup_order"][valp.A_MUTATING] is True


def test_events(spans) -> None:
    with valp.interaction("a") as ix:
        valp.approval_request("issue_refund")
        valp.approval_decision("denied", "issue_refund")
        valp.guardrail_trip("block")
    got = spans()
    assert got["valp.human_approval_request"][valp.A_OPERATION] == "human_approval_request"
    dec = got["valp.human_approval_decision"]
    assert (dec[valp.A_OPERATION], dec[valp.A_OUTCOME], dec[valp.A_INTERACTION]) == \
        ("human_approval_decision", "denied", ix.interaction_id)
    trip = got["valp.guardrail_trip"]
    assert trip[valp.A_GUARDRAIL] == "block" and not [k for k in trip if k.startswith("valp.guardrail_name")]


@pytest.mark.parametrize("call", [
    lambda: valp.tool_call("x", authority="supervised").__enter__(),
    lambda: valp.approval_decision("maybe"),
    lambda: valp.approval_decision(None),  # type: ignore[arg-type]
    lambda: valp.guardrail_trip("allow"),
    lambda: valp.set_context_class("has space"),
    lambda: valp.set_context_class("x" * 33),
    lambda: valp.set_context_class("naïve"),
])
def test_values_outside_the_vocabulary_are_refused(call) -> None:
    with pytest.raises(ValueError):
        call()


def test_context_class(spans) -> None:
    with trace.get_tracer("x").start_as_current_span("chat"):
        valp.set_context_class("billing.refund")
    assert spans()["chat"][valp.A_CONTEXT_CLASS] == "billing.refund"


@pytest.mark.parametrize("bad", [
    {"v": 2, "tools": []},
    {"v": 1, "tools": [{"name": "a", "is_external": True, "mutation": "fixed"}]},
    {"v": 1, "tools": [{"name": "a", "is_external": True, "is_mutating": True, "mutation": "per_call"}]},
    {"v": 1, "tools": [{"name": "a", "is_external": "yes", "is_mutating": True, "mutation": "fixed"}]},
])
def test_catalogue_rejects_malformed(bad) -> None:
    with pytest.raises(ValueError):
        valp.ToolCatalogue.from_dict(bad)


def test_catalogue_load(tmp_path) -> None:
    p = tmp_path / "tool_catalogue.confirmed.json"
    p.write_text(json.dumps(CATALOGUE))
    assert valp.ToolCatalogue.load(p).get("issue_refund") == valp.CatalogueEntry(True, True)


def test_approval_scope_on_the_decision(spans) -> None:
    valp.approval_decision("granted", "issue_refund", scope="standing")
    assert spans()["valp.human_approval_decision"][valp.A_APPROVAL_SCOPE] == "standing"
    with pytest.raises(ValueError):
        valp.approval_decision("granted", scope="forever")


def test_previous_interaction_marks_the_join(spans) -> None:
    valp.end_interaction("a", valp.new_ulid(), 1, "completed", previous_interaction_id="01M39GENKNQM7E9GYSDCAS6GJD")
    assert spans()["invoke_agent a"][valp.A_PREVIOUS_INTERACTION] == "01M39GENKNQM7E9GYSDCAS6GJD"


def test_vocabulary_is_every_valp_attribute() -> None:
    constants = {v for k, v in vars(valp).items() if k.startswith("A_") and isinstance(v, str)}
    assert valp.VOCABULARY == constants and all(k.startswith("valp.") for k in constants)
