"""redberry_sdk.hasher: the span processor's finish rules, its configuration, and the Azure Functions
decorator, end to end against fake HTTP (VALP and the store), with every report checked by VALP's rules."""
from __future__ import annotations

import asyncio
import dataclasses
import gzip
import inspect
import json
import threading
import time
from typing import Any

import pytest

pytest.importorskip("cryptography")

from opentelemetry import trace as api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind, StatusCode

import redberry_sdk._sdk as sdk
from redberry_sdk.hasher import (
    RedberryProcessor,
    TraceResult,
    _azure,
    register,
    resolve_config,
    with_redberry,
)
from redberry_sdk.hasher._http import Request, Response

from .test_sdk_hasher import AGENT, SIG, TOKEN, valp_accepts

OTHER_AGENT = "AGT-01K5V8QJ3M7XW2ZB4YRNCDEFGJ"
PUBLIC_KEY = bytes.fromhex(SIG["public_key_hex"])
ENV = {"REDBERRY_URL": "https://valp.example", "REDBERRY_PROJECT_TOKEN": TOKEN,
       "REDBERRY_SIGNING_KEY": SIG["signing_key_pkcs8_b64"], "REDBERRY_AGENT_ID": AGENT,
       "WEBSITE_SITE_NAME": "refund-func"}


class Valp:
    """VALP and Redberry's bucket: upload-url, the signed PUT, and the hash report (checked as VALP does)."""

    def __init__(self, report_status: int = 201, put_status: int = 200) -> None:
        self.report_status, self.put_status = report_status, put_status
        self.requests: list[Request] = []
        self.stored: dict[str, bytes] = {}
        self.reports: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def __call__(self, req: Request) -> Response:
        with self.lock:
            self.requests.append(req)
        if req.url.endswith("/stored-logs/upload-url"):
            run = json.loads(req.body)["run_id"]
            return Response(200, json.dumps({"url": f"https://bucket.example/{run}", "method": "PUT", "object": run,
                                             "headers": {"content-type": "application/gzip"}}).encode())
        if req.url.startswith("https://bucket.example/"):
            self.stored[req.url.rsplit("/", 1)[1]] = req.body
            return Response(self.put_status)
        if req.url.endswith("/hashes"):
            agent = req.url.split("/v1/agents/")[1].split("/")[0]
            self.reports.append(valp_accepts(agent, req.headers, req.body, PUBLIC_KEY))
            return Response(self.report_status, b'{"leaf":"hashes/x","window_start":"2026-10-22T00:00:00.000000Z"}')
        raise AssertionError(f"unexpected {req.method} {req.url}")

    def document(self, trace_id: str) -> dict[str, Any]:
        return json.loads(gzip.decompress(self.stored[trace_id]))


@pytest.fixture
def valp() -> Valp:
    return Valp()


def _processor(valp: Valp, results: list[TraceResult] | None = None, env: dict[str, str] | None = None,
               **options: Any) -> RedberryProcessor:
    options.setdefault("grace_ms", 30)
    return RedberryProcessor(env={**ENV, **(env or {})}, transport=valp, retry_delays=(0, 0),
                             on_result=(results.append if results is not None else None), **options)


def _provider(proc: RedberryProcessor) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "refund-func"}))
    provider.add_span_processor(proc)
    return provider


def _wait(cond: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def _chat(tracer: Any, name: str = "chat gpt-4o") -> Any:
    return tracer.start_as_current_span(name, kind=SpanKind.CLIENT, attributes={
        "gen_ai.operation.name": "chat", "gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 3})


# ── the processor ────────────────────────────────────────────────────────────────────────────────


def test_a_finished_trace_is_stored_and_reported_after_the_grace_period(valp):
    results: list[TraceResult] = []
    proc = _processor(valp, results)
    tracer = _provider(proc).get_tracer("t")
    with tracer.start_as_current_span("POST /api/refund", attributes={"url.path": "/api/refund"}) as root, \
            _chat(tracer):
        pass
    _wait(lambda: results)
    (r,) = results
    assert (r.finish, r.cut_reason, r.stored, r.report.outcome, r.agent_id) == ("complete", None, True, "recorded", AGENT)
    tid = format(root.get_span_context().trace_id, "032x")
    doc = valp.document(tid)
    assert doc["schema"] == "redberry.trace.v1" and doc["producer"]["name"] == "redberry-sdk"
    assert [s["name"] for s in doc["spans"]] == ["POST /api/refund", "chat gpt-4o"]
    assert doc["spans"][1]["parentSpanId"] == doc["spans"][0]["spanId"] and doc["spans"][1]["kind"] == 2
    assert doc["spans"][1]["attributes"]["gen_ai.usage.input_tokens"] == 12
    assert doc["resource"]["service.name"] == "refund-func"
    (report,) = valp.reports
    assert report["sha256"] == r.sha256 and report["store"] == "redberry" and report["stored"] is True
    assert (report["started_at"], report["ended_at"]) == (doc["started_at"], doc["ended_at"])
    assert proc.stats()["recorded"] == 1 and proc.stats()["open_traces"] == 0


def test_a_span_starting_in_the_grace_period_resets_it(valp):
    results: list[TraceResult] = []
    proc = _processor(valp, results, grace_ms=150)
    tracer = _provider(proc).get_tracer("t")
    with tracer.start_as_current_span("root") as root:
        pass
    time.sleep(0.05)
    with tracer.start_as_current_span("chat", context=api.set_span_in_context(root),
                                      attributes={"gen_ai.operation.name": "chat"}):
        time.sleep(0.2)
    _wait(lambda: results)
    assert results[0].finish == "complete" and results[0].span_count == 2


def test_a_trace_without_model_spans_is_not_hashed(valp):
    results: list[TraceResult] = []
    tracer = _provider(_processor(valp, results)).get_tracer("t")
    with tracer.start_as_current_span("GET /health"):
        pass
    _wait(lambda: results)
    assert results[0].skipped == "no_ai_spans" and not valp.requests


def test_openinference_spans_count_as_model_spans(valp):
    results: list[TraceResult] = []
    tracer = _provider(_processor(valp, results)).get_tracer("openinference.instrumentation.anthropic")
    with tracer.start_as_current_span("Messages", attributes={"openinference.span.kind": "LLM"}):
        pass
    _wait(lambda: results)
    assert results[0].skipped is None and results[0].report.outcome == "recorded"


def test_hard_stop_cuts_a_trace_whose_spans_stay_open(valp):
    results: list[TraceResult] = []
    tracer = _provider(_processor(valp, results, hard_stop_ms=50)).get_tracer("t")
    with tracer.start_as_current_span("root"):
        stream = tracer.start_span("chat", attributes={"gen_ai.operation.name": "chat"})
    _wait(lambda: results)
    r = results[0]
    assert (r.finish, r.cut_reason) == ("cut_short", "hard_stop")
    open_span = next(s for s in valp.document(r.trace_id)["spans"] if s["name"] == "chat")
    assert open_span["endTimeUnixNano"] is None
    stream.end()
    assert valp.reports[0]["trace_id"] == r.trace_id and len(valp.reports) == 1


def test_late_spans_are_counted_and_never_make_a_second_document(valp):
    results: list[TraceResult] = []
    proc = _processor(valp, results, hard_stop_ms=30)
    tracer = _provider(proc).get_tracer("t")
    with tracer.start_as_current_span("root"):
        late = tracer.start_span("chat", attributes={"gen_ai.operation.name": "chat"})
    _wait(lambda: results)
    late.end()
    assert proc.stats()["late_spans"] == 1 and len(valp.reports) == 1


def test_the_deadline_cuts_a_trace_before_the_function_is_killed(valp):
    results: list[TraceResult] = []
    tracer = _provider(_processor(valp, results, deadline_ms=200, deadline_margin_ms=150)).get_tracer("t")
    with tracer.start_as_current_span("root"), _chat(tracer):
        _wait(lambda: results)
    r = results[0]
    assert (r.finish, r.cut_reason, r.report.outcome) == ("cut_short", "deadline", "recorded")
    assert [s["endTimeUnixNano"] for s in valp.document(r.trace_id)["spans"]] == [None, None]


def test_shutdown_cuts_open_traces_and_waits_for_them(valp):
    results: list[TraceResult] = []
    proc = _processor(valp, results, grace_ms=60_000)
    tracer = _provider(proc).get_tracer("t")
    with tracer.start_as_current_span("root"), _chat(tracer):
        pass
    proc.shutdown()  # the finish rule already held: complete
    root2 = tracer.start_span("root2")
    tracer.start_span("chat", attributes={"gen_ai.operation.name": "chat"},
                      context=api.set_span_in_context(root2)).end()
    proc.shutdown()
    assert [(r.finish, r.cut_reason) for r in results] == [("complete", None), ("cut_short", "shutdown")]


def test_a_failed_store_still_reports_with_stored_false():
    valp = Valp(put_status=403)
    results: list[TraceResult] = []
    tracer = _provider(_processor(valp, results)).get_tracer("t")
    with tracer.start_as_current_span("root"), _chat(tracer):
        pass
    _wait(lambda: results)
    assert results[0].stored is False and "HTTP 403" in results[0].store_error
    assert valp.reports[0]["stored"] is False and results[0].report.outcome == "recorded"


def test_a_refused_report_is_counted_not_retried():
    valp = Valp(report_status=409)
    results: list[TraceResult] = []
    proc = _processor(valp, results)
    tracer = _provider(proc).get_tracer("t")
    with tracer.start_as_current_span("root"), _chat(tracer):
        pass
    _wait(lambda: results)
    assert results[0].report.outcome == "refused" and proc.stats()["refused"] == 1
    assert sum(r.url.endswith("/hashes") for r in valp.requests) == 1


def test_the_agent_comes_from_the_root_span_then_the_route_map_then_the_default(valp):
    results: list[TraceResult] = []
    routes = json.dumps({"/api/claims/*": OTHER_AGENT})
    tracer = _provider(_processor(valp, results, env={"REDBERRY_AGENTS": routes})).get_tracer("t")
    for attrs in ({"url.path": "/api/claims/7"}, {"url.path": "/api/refund"},
                  {"url.path": "/api/claims/7", "redberry.agent_id": AGENT},
                  {"url.path": "/api/claims/7", "redberry.agent_id": "not-an-agent"}):
        with tracer.start_as_current_span("root", attributes=attrs), _chat(tracer):
            pass
    _wait(lambda: len(results) == 4)
    assert sorted(r.agent_id for r in results) == sorted([OTHER_AGENT, AGENT, AGENT, OTHER_AGENT])


def test_no_agent_means_not_hashed(valp):
    results: list[TraceResult] = []
    tracer = _provider(_processor(valp, results, env={"REDBERRY_AGENT_ID": ""})).get_tracer("t")
    with tracer.start_as_current_span("root"), _chat(tracer):
        pass
    _wait(lambda: results)
    assert results[0].skipped == "no_agent" and not valp.requests


# ── configuration ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("env", "enabled"), [
    ({}, True),                                                              # WEBSITE_SITE_NAME: Azure
    ({"REDBERRY_DISABLED": "1"}, False),
    ({"WEBSITE_SITE_NAME": ""}, False),
    ({"WEBSITE_SITE_NAME": "", "VERCEL_ENV": "preview"}, False),
    ({"WEBSITE_SITE_NAME": "", "VERCEL_ENV": "production"}, True),
    ({"WEBSITE_SITE_NAME": "", "REDBERRY_ALL_ENVS": "1"}, True),
])
def test_where_the_hasher_runs(env, enabled):
    assert resolve_config(env={**ENV, **env}).enabled is enabled


@pytest.mark.parametrize(("env", "reason"), [
    ({"REDBERRY_URL": ""}, "REDBERRY_URL"),
    ({"REDBERRY_PROJECT_TOKEN": "x"}, "REDBERRY_PROJECT_TOKEN"),
    ({"REDBERRY_SIGNING_KEY": ""}, "REDBERRY_SIGNING_KEY"),
    ({"REDBERRY_KEY_ID": "abc"}, "REDBERRY_KEY_ID"),
    ({"REDBERRY_AGENT_ID": "agent-1"}, "not agent ids"),
    ({"REDBERRY_AGENTS": "[1]"}, "REDBERRY_AGENTS"),
    ({"REDBERRY_STORE": "gcs"}, "Vercel"),
    ({"REDBERRY_STORE": "azure-blob", "REDBERRY_AZURE_CONTAINER_URL": "https://a.blob.core.windows.net/c"},
     "IDENTITY_ENDPOINT"),
])
def test_misconfiguration_disables_with_a_reason_and_never_raises(env, reason):
    cfg = resolve_config(env={**ENV, **env})
    assert not cfg.enabled and reason in cfg.disabled_reason


def test_azure_blob_from_the_environment_prefers_the_redberry_client_id():
    cfg = resolve_config(env={**ENV, "REDBERRY_STORE": "azure-blob",
                              "REDBERRY_AZURE_CONTAINER_URL": "https://a.blob.core.windows.net/runs",
                              "IDENTITY_ENDPOINT": "http://localhost/msi", "IDENTITY_HEADER": "h",
                              "REDBERRY_AZURE_CLIENT_ID": "redberry-mi", "AZURE_CLIENT_ID": "app-mi"})
    assert cfg.enabled and cfg.store.kind == "azure-blob" and cfg.store.credential.client_id == "redberry-mi"


# ── Azure Functions: with_redberry and register ──────────────────────────────────────────────────


class FakeRequest:
    """The parts of ``azure.functions.HttpRequest`` the decorator reads."""

    def __init__(self, url: str, method: str = "POST") -> None:
        self.url, self.method, self.route_params = url, method, {}

    def get_body(self) -> bytes:
        return b"{}"


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeTraceContext:
    trace_parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"  # not sampled
    trace_state = ""


class FakeContext:
    invocation_id = "e3b5d6b8-0000-4000-8000-000000000001"
    function_name = "refund"
    trace_context = FakeTraceContext()


@pytest.fixture
def installed(valp, monkeypatch):
    """register() with a processor of ours, on a provider of ours (the global one is left alone)."""
    provider = TracerProvider(resource=Resource.create({"service.name": "refund-func"}))
    monkeypatch.setattr(_azure, "_processor", None)
    monkeypatch.setattr(_azure, "_provider", None)
    monkeypatch.setattr(_azure.trace, "get_tracer_provider", lambda: provider)
    calls: list[str] = []
    monkeypatch.setattr(sdk, "prepare_instrumentation", lambda **k: calls.append(f"prepare {k}"))
    monkeypatch.setattr(sdk, "enable_instrumentation", lambda **k: calls.append(f"enable {k}") or [])
    proc = register(processor=_processor(valp, grace_ms=1000))
    return proc, provider, calls


def _llm_call(provider: TracerProvider) -> None:
    with _chat(provider.get_tracer("opentelemetry.instrumentation.openai_v2")):
        pass


def test_register_adds_the_hasher_to_the_provider_and_turns_on_the_sdks_instrumentation(installed):
    proc, provider, calls = installed
    assert proc in provider._active_span_processor._span_processors
    assert calls == [("prepare {'capture_content': True, 'allow_missing_instrumentation': False, "
                      "'allow_content_capture_off': False}"), "enable {'allow_missing_instrumentation': False}"]
    assert register() is proc


def test_a_sync_handler_is_stored_and_reported_before_it_returns(installed, valp):
    proc, provider, _ = installed

    @with_redberry
    def refund(req, context):
        _llm_call(provider)
        return FakeResponse(200)

    resp = refund(req=FakeRequest("https://refund-func.azurewebsites.net/api/refund?code=SECRET&x=1"),
                  context=FakeContext())
    assert resp.status_code == 200
    (report,) = valp.reports  # already there: no waiting
    doc = valp.document(report["trace_id"])
    assert doc["finish"] == "complete" and doc["agent_id"] == AGENT
    root = doc["spans"][0]
    assert root["parentSpanId"] is None and root["kind"] == 1 and root["name"] == "refund"
    a = root["attributes"]
    assert a["faas.name"] == "refund" and a["faas.trigger"] == "http"
    assert a["faas.invocation_id"] == FakeContext.invocation_id and a["http.request.method"] == "POST"
    assert a["url.path"] == "/api/refund" and a["http.response.status_code"] == 200
    assert a["redberry.agent_id"] == AGENT
    assert "SECRET" not in gzip.decompress(valp.stored[report["trace_id"]]).decode()
    # The host's trace is linked, not parented: it was not sampled, and the run is still hashed.
    assert root["links"][0]["traceId"] == "0af7651916cd43dd8448eb211c80319c"
    assert root["traceId"] != "0af7651916cd43dd8448eb211c80319c"
    assert proc.stats()["open_traces"] == 0


def test_the_decorators_agent_wins(installed, valp):
    _, provider, _ = installed

    @with_redberry(agent_id=OTHER_AGENT)
    def handler(req):
        _llm_call(provider)
        return FakeResponse(201)

    handler(FakeRequest("https://x/api/refund"))
    assert valp.reports[0]["agent_id"] == OTHER_AGENT
    assert valp.document(valp.reports[0]["trace_id"])["spans"][0]["attributes"]["redberry.agent_id"] == OTHER_AGENT


def test_an_async_handler_is_stored_and_reported_before_it_returns(installed, valp):
    _, provider, _ = installed

    @with_redberry
    async def handler(req):
        await asyncio.sleep(0)
        _llm_call(provider)
        return FakeResponse(200)

    assert inspect.iscoroutinefunction(handler)
    assert asyncio.run(handler(req=FakeRequest("https://x/api/a"))).status_code == 200
    assert len(valp.reports) == 1


def test_the_handlers_exception_propagates_after_the_run_is_reported(installed, valp):
    _, provider, _ = installed

    @with_redberry
    def handler(req):
        _llm_call(provider)
        raise KeyError("boom")

    with pytest.raises(KeyError, match="boom"):
        handler(FakeRequest("https://x/api/a"))
    root = valp.document(valp.reports[0]["trace_id"])["spans"][0]
    assert root["status"] == {"code": StatusCode.ERROR.value, "message": "KeyError: 'boom'"}
    assert root["events"][0]["name"] == "exception"


def test_a_span_left_open_by_the_handler_cuts_the_run_short(installed, valp, monkeypatch):
    proc, provider, _ = installed
    monkeypatch.setattr(proc, "cfg", dataclasses.replace(proc.cfg, grace_ms=20))
    leaked: list[Any] = []

    @with_redberry
    def handler(req):
        leaked.append(provider.get_tracer("t").start_span("chat", attributes={"gen_ai.operation.name": "chat"}))
        return FakeResponse(200)

    handler(FakeRequest("https://x/api/a"))
    doc = valp.document(valp.reports[0]["trace_id"])
    assert (doc["finish"], doc["cut_reason"]) == ("cut_short", "hard_stop")
    leaked[0].end()


def test_the_handlers_signature_is_kept_for_the_functions_host():
    def main(req: FakeRequest, context: FakeContext) -> FakeResponse:
        return FakeResponse(200)

    wrapped = with_redberry(main)
    assert inspect.signature(wrapped) == inspect.signature(main) and wrapped.__name__ == "main"


def test_a_bad_agent_id_is_refused_when_decorating():
    with pytest.raises(ValueError, match="AGT-"):
        with_redberry(agent_id="refund-agent")


def test_disabled_the_handler_just_runs(monkeypatch):
    monkeypatch.setattr(_azure, "_processor", None)
    monkeypatch.setattr(_azure, "_provider", None)
    proc = register(processor=RedberryProcessor(env={**ENV, "REDBERRY_DISABLED": "1"}))
    assert not proc.enabled

    @with_redberry
    def handler(req):
        return "ok"

    assert handler(FakeRequest("https://x/")) == "ok"


def test_a_refused_lazy_registration_leaves_the_handler_running(monkeypatch):
    monkeypatch.setattr(_azure, "_processor", None)

    def refuse(**_: Any) -> Any:
        raise sdk.MissingInstrumentationError("openai without its instrumentation")

    monkeypatch.setattr(_azure, "register", refuse)

    @with_redberry
    def handler(req):
        return "ok"

    assert handler(FakeRequest("https://x/")) == "ok"
    assert not _azure.get_processor().enabled
