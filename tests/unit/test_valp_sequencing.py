"""valp.emitter_id / valp.span_seq: every exported span numbered, contiguously, at export."""

from __future__ import annotations

import threading

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from redberry_sdk import valp


def _provider(exporter: SpanExporter) -> TracerProvider:
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp


def test_numbers_are_contiguous_and_carry_the_emitter() -> None:
    inner = InMemorySpanExporter()
    seq = valp.ValpSequencingExporter(inner)
    tracer = _provider(seq).get_tracer("t")
    for i in range(5):
        with tracer.start_as_current_span(f"s{i}"):
            pass
    spans = inner.get_finished_spans()
    assert [s.attributes[valp.A_SPAN_SEQ] for s in spans] == [0, 1, 2, 3, 4]
    assert {s.attributes[valp.A_EMITTER] for s in spans} == {seq.emitter_id}
    assert len(seq.emitter_id) == 26


def test_the_span_is_otherwise_unchanged() -> None:
    inner = InMemorySpanExporter()
    tracer = _provider(valp.ValpSequencingExporter(inner)).get_tracer("scope-x")
    with tracer.start_as_current_span("original", attributes={"k": "v"}) as sp:
        sp.add_event("e", {"a": 1})
        sp.add_link(sp.get_span_context())
        sp.set_status(Status(StatusCode.ERROR, "why"))
    (out,) = inner.get_finished_spans()
    assert out.name == "original" and out.attributes["k"] == "v"
    assert [e.name for e in out.events] == ["e"] and len(out.links) == 1
    assert out.status.status_code == StatusCode.ERROR and out.instrumentation_scope.name == "scope-x"
    assert out.end_time >= out.start_time


def test_a_failed_export_keeps_its_numbers_so_the_loss_shows_as_a_gap() -> None:
    class Flaky(SpanExporter):
        def __init__(self):
            self.calls, self.got = 0, []

        def export(self, spans):
            self.calls += 1
            if self.calls == 2:
                return SpanExportResult.FAILURE
            self.got += [s.attributes[valp.A_SPAN_SEQ] for s in spans]
            return SpanExportResult.SUCCESS

    inner = Flaky()
    tracer = _provider(valp.ValpSequencingExporter(inner)).get_tracer("t")
    for i in range(3):
        with tracer.start_as_current_span(f"s{i}"):
            pass
    assert inner.got == [0, 2]  # seq 1 was lost, and it shows


def test_concurrent_exports_never_share_a_number() -> None:
    inner = InMemorySpanExporter()
    tracer = _provider(valp.ValpSequencingExporter(inner)).get_tracer("t")

    def work():
        for _ in range(50):
            with tracer.start_as_current_span("x"):
                pass
    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s.attributes[valp.A_SPAN_SEQ] for s in inner.get_finished_spans()) == list(range(400))


def test_a_span_marked_not_run_takes_no_number_so_leaves_no_gap() -> None:
    """init() puts ValpToolRanMarker outside the numbering. A span for a call that never ran is
    exported (the normaliser and VALP both get it) but is not a counted event, so it takes no number."""
    inner = InMemorySpanExporter()
    tracer = _provider(valp.ValpToolRanMarker(valp.ValpSequencingExporter(inner))).get_tracer("t")
    valp.watch_tool("seq_probe")
    tool = {"openinference.span.kind": "TOOL", "tool.name": "seq_probe"}
    with tracer.start_as_current_span("a"):
        pass
    with tracer.start_as_current_span("seq_probe", attributes=tool):
        pass  # paused for approval: the tool never ran
    with tracer.start_as_current_span("seq_probe", attributes=tool) as ran:
        valp.tool_ran(ran)
    got = [(s.name, s.attributes.get(valp.A_TOOL_RAN), s.attributes.get(valp.A_SPAN_SEQ), valp.A_EMITTER in s.attributes)
           for s in inner.get_finished_spans()]
    assert got == [("a", None, 0, True), ("seq_probe", False, None, True), ("seq_probe", None, 1, True)]
