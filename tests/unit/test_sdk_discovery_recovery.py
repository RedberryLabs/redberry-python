"""Discovery failures are loud and coded, and the SDK recovers from them without a restart.

A failed discovery used to log one WARNING and send to localhost for the life of the process, so an agent
granted after it started never reached its install until it was redeployed.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult

from redberry_sdk import _emit


@pytest.fixture(autouse=True)
def _clean():
    _emit._reset_for_tests()
    with _emit._ONCE_LOCK:
        _emit._LOGGED_ONCE.clear()
    yield
    _emit._reset_for_tests()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeIngest:
    def __init__(self) -> None:
        self.batches = []

    def export(self, spans):
        self.batches.append(list(spans))
        return SpanExportResult.SUCCESS

    def stop(self): ...
    def flush_deadline(self, until): ...
    def shutdown(self): ...
    def force_flush(self, timeout_millis=30000):
        return True


def test_gcp_without_a_region_is_an_error_with_its_code(monkeypatch, caplog):
    monkeypatch.setattr(_emit, "_metadata_get", lambda path, timeout=1.0: "123" if "project" in path else None)
    monkeypatch.setattr(_emit, "discover_aws", lambda: None)
    monkeypatch.setattr(_emit, "discover_azure", lambda: None)
    with caplog.at_level(logging.ERROR, logger="redberry.sdk"):
        ep = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    assert ep.source == "undiscovered" and _emit.discovery_failure() == "RB-SDK-DISC-001"
    assert any(r.levelno == logging.ERROR and "RB-SDK-DISC-001" in r.getMessage() and _emit.REGION_ENV in r.getMessage()
               for r in caplog.records)


def test_named_but_on_no_cloud_says_so(monkeypatch, caplog):
    for f in ("discover_aws", "discover_azure", "discover_gcp"):
        monkeypatch.setattr(_emit, f, lambda: None)
    with caplog.at_level(logging.WARNING, logger="redberry.sdk"):
        ep = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    assert ep.source == "default" and ep.url == _emit.DEFAULT_ENDPOINT
    assert "RB-SDK-DISC-004" in caplog.text and _emit.INGEST_URL_ENV in caplog.text


def test_unnamed_agents_never_discover_or_warn(monkeypatch, caplog):
    called = []
    monkeypatch.setattr(_emit, "discover_install", lambda: called.append(1))
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=False).source == "default"
    assert not called and "RB-SDK" not in caplog.text


def test_the_exporter_retries_discovery_and_switches_to_ingest(monkeypatch, caplog):
    clock, ingest, attempts = Clock(), FakeIngest(), []
    found = _emit.Endpoint(url="https://redberry-ingest.example", source="gcp", audience="x")
    answers = iter([None, found])

    def discover():
        attempts.append(clock.t)
        return next(answers)

    monkeypatch.setattr(_emit, "discover_install", discover)
    monkeypatch.setattr(_emit, "_ingest_exporter", lambda endpoint: ingest)
    _emit._DISCOVERY_FAILURE = "RB-SDK-DISC-002"
    ex = _emit.RediscoveringExporter(retry_s=300, clock=clock)

    assert ex.export(["a"]) is SpanExportResult.FAILURE and attempts == []        # too soon: no attempt
    clock.t += 300
    assert ex.export(["b"]) is SpanExportResult.FAILURE and len(attempts) == 1    # tried, still not found
    clock.t += 100
    assert ex.export(["c"]) is SpanExportResult.FAILURE and len(attempts) == 1    # waits the full interval
    clock.t += 200
    with caplog.at_level(logging.WARNING, logger="redberry.sdk"):
        assert ex.export(["d"]) is SpanExportResult.SUCCESS                       # found: switched over
    assert ingest.batches == [["d"]] and len(attempts) == 2
    assert "RB-SDK-DISC-002 resolved" in caplog.text and "about 10 min" in caplog.text
    assert ex.export(["e"]) is SpanExportResult.SUCCESS and len(attempts) == 2    # no more discovery
    assert _emit.recent_recovery(now=clock.t + 60) == ("RB-SDK-DISC-002", 10)
    assert _emit.recent_recovery(now=clock.t + 3600) is None                      # shown for an hour


def test_the_marker_stamps_new_spans_for_an_hour_after_recovery(monkeypatch):
    class Span:
        def __init__(self) -> None:
            self.attributes = {}

        def set_attribute(self, k, v):
            self.attributes[k] = v

    marker = _emit.DiscoveryFailureMarker()
    plain = Span()
    marker.on_start(plain)
    assert plain.attributes == {}
    monkeypatch.setattr(_emit, "recent_recovery", lambda now=None: ("RB-SDK-DISC-003", 25))
    marked = Span()
    marker.on_start(marked)
    assert marked.attributes == {_emit.DISCOVERY_FAILURE_ATTRIBUTE: "RB-SDK-DISC-003;25"}


def test_a_stopped_exporter_stops_looking(monkeypatch):
    clock, attempts = Clock(), []
    monkeypatch.setattr(_emit, "discover_install", lambda: attempts.append(1))
    ex = _emit.RediscoveringExporter(retry_s=5, clock=clock)
    ex.shutdown()
    clock.t += 60
    assert ex.export(["a"]) is SpanExportResult.FAILURE and attempts == []
    assert ex.force_flush() is True


@pytest.mark.parametrize("raw, want", [("", 300.0), ("60", 60.0), ("1", 5.0), ("soon", 300.0)])
def test_the_retry_interval_setting(monkeypatch, raw, want):
    monkeypatch.setenv(_emit.DISCOVERY_RETRY_ENV, raw)
    assert _emit.discovery_retry_s() == want
