"""The agent's emit path (redberry_sdk._emit): endpoint discovery on GCP, the ID token on every
export, the bounded queue and its dropped-span count, and nothing of it ever raising into the agent.

A fake metadata server runs on a local port (``GCE_METADATA_HOST``), and a fake ingest records the
requests the real OTLP/HTTP exporter sends it.
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

import redberry_sdk
from redberry_sdk import _emit, _sdk


def _jwt(exp: float) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256'})}.{part({'exp': exp, 'aud': 'x'})}.sig"


class _Server:
    """A tiny HTTP server: ``routes`` maps a path (without the query) to (status, body, headers)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.requests.append((self.path, dict(self.headers)))
                route = outer.routes.get(self.path.split("?")[0])
                if callable(route):
                    route = route(self)
                status, body, headers = route or (404, "", {})
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


G = {"Metadata-Flavor": "Google"}
MD = "/computeMetadata/v1/"


def metadata_routes(region="projects/123456/regions/europe-west2", token=None):
    routes = {MD + "project/numeric-project-id": (200, "123456", G),
              MD + "project/project-id": (200, "acme-prod", G)}
    if region is not None:
        routes[MD + "instance/region"] = (200, region, G)
    routes[MD + "instance/service-accounts/default/identity"] = (
        (200, token, G) if token else (404, "", G))
    return routes


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in (_emit.ENDPOINT_ENV, _emit.REGION_ENV, _emit.METADATA_HOST_ENV, "K_SERVICE",
                 "K_REVISION"):
        monkeypatch.delenv(name, raising=False)
    _emit._reset_for_tests()
    yield
    _emit._reset_for_tests()


@pytest.fixture
def metadata(monkeypatch):
    servers = []

    def start(**kw):
        server = _Server(metadata_routes(**kw))
        servers.append(server)
        monkeypatch.setenv(_emit.METADATA_HOST_ENV, server.host)
        return server

    yield start
    for s in servers:
        s.close()


@pytest.fixture
def no_metadata(monkeypatch):
    """A metadata host where nothing listens: off GCP."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv(_emit.METADATA_HOST_ENV, f"127.0.0.1:{port}")


# ── agent name ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["a", "support-bot", "Claims Triage v2", "x" * 64, "café"])
def test_valid_names(name):
    assert _emit.validate_agent_name(name) == name


@pytest.mark.parametrize("name", ["", "x" * 65, "a\nb", "tab\there", " lead", "trail ", None, 7])
def test_invalid_names(name):
    with pytest.raises((ValueError, TypeError)):
        _emit.validate_agent_name(name)


# ── discovery ────────────────────────────────────────────────────────────────────────────────────

def test_url_building():
    assert _emit.ingest_url("123456", "europe-west2") == "https://redberry-ingest-123456.europe-west2.run.app"


def test_cloud_run_discovery(metadata):
    server = metadata()
    endpoint = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    assert endpoint == _emit.Endpoint("https://redberry-ingest-123456.europe-west2.run.app", "gcp",
                                      "https://redberry-ingest-123456.europe-west2.run.app")
    assert all(h.get("Metadata-Flavor") == "Google" for _, h in server.requests)


def test_discovery_is_cached(metadata):
    server = metadata()
    first = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    n = len(server.requests)
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True) is first
    assert len(server.requests) == n


def test_explicit_env_wins_over_gcp(metadata, monkeypatch):
    server = metadata()
    monkeypatch.setenv(_emit.ENDPOINT_ENV, "http://collector:4318")
    endpoint = _emit.resolve_endpoint("http://collector:4318", discover=True)
    assert endpoint == _emit.Endpoint("http://collector:4318", "explicit")
    assert server.requests == []


def test_explicit_kwarg_wins_over_gcp(metadata):
    metadata()
    assert _emit.resolve_endpoint("https://ingest.example/", discover=True).url == "https://ingest.example"


def test_env_set_to_localhost_is_still_explicit(metadata, monkeypatch):
    server = metadata()
    monkeypatch.setenv(_emit.ENDPOINT_ENV, _emit.DEFAULT_ENDPOINT)
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True).source == "explicit"
    assert server.requests == []


def test_unnamed_agent_keeps_localhost_even_on_gcp(metadata):
    server = metadata()
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=False) == _emit.Endpoint(
        _emit.DEFAULT_ENDPOINT, "default")
    assert server.requests == []


def test_off_gcp_falls_back_to_localhost(no_metadata):
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True) == _emit.Endpoint(
        _emit.DEFAULT_ENDPOINT, "default")


def test_a_server_without_the_google_header_is_not_gcp(monkeypatch):
    server = _Server({MD + "project/numeric-project-id": (200, "123456", {})})
    monkeypatch.setenv(_emit.METADATA_HOST_ENV, server.host)
    try:
        assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True).source == "default"
    finally:
        server.close()


def test_region_absent_needs_a_setting_and_logs_once(metadata, caplog):
    metadata(region=None)
    with caplog.at_level("WARNING", logger="redberry.sdk"):
        assert _emit.discover_gcp() is None
        assert _emit.discover_gcp() is None
    assert [r.message for r in caplog.records].count(caplog.records[0].message) == 1
    assert _emit.REGION_ENV in caplog.records[0].message


def test_region_setting_on_gce(metadata, monkeypatch):
    metadata(region=None)
    monkeypatch.setenv(_emit.REGION_ENV, "us-central1")
    assert _emit.discover_gcp().url == "https://redberry-ingest-123456.us-central1.run.app"


# ── the ID token ─────────────────────────────────────────────────────────────────────────────────

def test_token_cached_until_five_minutes_before_expiry(metadata):
    now = [1_000_000.0]
    server = metadata(token=_jwt(now[0] + 3600))
    auth = _emit.IdTokenAuth("https://redberry-ingest-1.r.run.app", clock=lambda: now[0])
    token = auth.token()
    path, headers = server.requests[-1]
    assert "audience=https%3A%2F%2Fredberry-ingest-1.r.run.app" in path and "format=full" in path
    assert headers["Metadata-Flavor"] == "Google"
    now[0] += 3600 - 301
    assert auth.token() == token and len(server.requests) == 1
    now[0] += 2  # inside the five-minute margin
    auth.token()
    assert len(server.requests) == 2


def test_no_token_is_a_retryable_connection_error_logged_once(metadata, caplog):
    import requests
    metadata(token=None)
    auth = _emit.IdTokenAuth("https://a")
    with caplog.at_level("WARNING", logger="redberry.sdk"):
        for _ in range(3):
            with pytest.raises(requests.exceptions.ConnectionError):
                auth.token()
    assert len([r for r in caplog.records if "ID token" in r.message]) == 1


def test_bearer_header_on_every_export(metadata):
    """The real OTLP/HTTP exporter, its session carrying the token, posting to a fake ingest."""
    md = metadata(token=_jwt(9_999_999_999))
    ingest = _Server({"/v1/traces": (200, "", {"Content-Type": "application/x-protobuf"})})
    try:
        endpoint = _emit.Endpoint(f"http://{ingest.host}", "gcp", "https://redberry-ingest-123456.r.run.app")
        processor = _emit.build_processor(endpoint)
        provider = TracerProvider(resource=Resource({"redberry.agent.name": "bot"}))
        provider.add_span_processor(processor)
        tracer = provider.get_tracer("t")
        for _ in range(2):
            with tracer.start_as_current_span("s"):
                pass
            provider.force_flush()
        provider.shutdown()
        posts = [h for p, h in ingest.requests if p == "/v1/traces"]
        assert len(posts) == 2
        assert all(h["Authorization"] == f"Bearer {_jwt(9_999_999_999)}" for h in posts)
        assert len([p for p, _ in md.requests if "identity" in p]) == 1  # cached across exports
        assert redberry_sdk.dropped_spans() == 0
    finally:
        ingest.close()


def test_no_header_off_gcp():
    assert _emit.build_session(_emit.Endpoint(_emit.DEFAULT_ENDPOINT, "default")) is None


# ── never raise, count what is dropped ───────────────────────────────────────────────────────────

class _Raising(SpanExporter):
    def export(self, spans):
        raise RuntimeError("boom")

    def shutdown(self):
        pass


class _Failing(SpanExporter):
    def export(self, spans):
        return SpanExportResult.FAILURE

    def shutdown(self):
        pass


def test_an_exporter_that_raises_never_reaches_the_agent():
    assert _emit.CountingExporter(_Raising()).export([object(), object()]) is SpanExportResult.FAILURE
    assert redberry_sdk.dropped_spans() == 2


def test_failed_exports_are_counted_and_logged(caplog):
    with caplog.at_level("WARNING", logger="redberry.sdk"):
        _emit.CountingExporter(_Failing()).export([object()] * 3)
    assert redberry_sdk.dropped_spans() == 3
    assert any("3 span(s) dropped" in r.message for r in caplog.records)


def test_dropped_log_is_periodic(caplog):
    with caplog.at_level("WARNING", logger="redberry.sdk"):
        _emit._count_dropped(1)
        _emit._count_dropped(1)  # within the interval: no second line
    assert len(caplog.records) == 1
    with caplog.at_level("WARNING", logger="redberry.sdk"):
        _emit._maybe_log_dropped(now=10**9)
    assert len(caplog.records) == 2 and "2 span(s)" in caplog.records[-1].message


def test_full_queue_drops_and_counts_without_blocking():
    class Blocked(SpanExporter):
        def __init__(self):
            self.release = threading.Event()

        def export(self, spans):
            self.release.wait(5)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            self.release.set()

    inner = Blocked()
    processor = _emit.CountingBatchSpanProcessor(
        _emit.CountingExporter(inner), max_queue_size=4, max_export_batch_size=1, schedule_delay_millis=10_000)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("t")
    for _ in range(20):
        tracer.start_span("s").end()
    # The worker holds at most one batch of one; the queue holds four; the rest were dropped.
    assert 20 - 4 - 1 <= redberry_sdk.dropped_spans() <= 20 - 4
    inner.release.set()
    provider.shutdown()


def test_no_token_and_unreachable_ingest_do_not_raise(no_metadata):
    """No metadata server (so no token) and nothing listening: the export fails, is counted, and
    nothing raises. The short timeout stands in for the exporter's own retry deadline."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    endpoint = _emit.Endpoint("http://127.0.0.1:9", "gcp", "https://a")
    exporter = _emit.CountingExporter(OTLPSpanExporter(
        endpoint=f"{endpoint.url}/v1/traces", session=_emit.build_session(endpoint), timeout=0.5))
    span = TracerProvider().get_tracer("t").start_span("s")
    span.end()
    assert exporter.export([span]) is SpanExportResult.FAILURE
    assert redberry_sdk.dropped_spans() == 1


# ── init(agent=...) ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def init_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("OTELLINE_AGENT_ID", "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
                 "OTELLINE_TRACE_SINK", "OTELLINE_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_sdk, "_INITIALIZED", False)
    monkeypatch.setattr(_sdk, "_load_otelline_dotenv", lambda: None)
    monkeypatch.setattr(_sdk, "enable_instrumentation", lambda **kw: [])
    monkeypatch.setattr(_sdk, "prepare_instrumentation", lambda **kw: None)
    providers = []
    monkeypatch.setattr(_sdk.trace, "set_tracer_provider", providers.append)
    yield providers
    for p in providers:
        p.shutdown()
    _sdk._INITIALIZED = False
    _sdk._TRACER_PROVIDER = None


def _otlp(provider):
    processors = provider._active_span_processor._span_processors
    batch = [p for p in processors if isinstance(p, _emit.CountingBatchSpanProcessor)]
    assert len(batch) == 1
    exporter = batch[0].span_exporter.inner  # CountingExporter → ValpToolRanMarker
    while hasattr(exporter, "_inner") or hasattr(exporter, "inner"):
        exporter = getattr(exporter, "_inner", None) or exporter.inner
    return exporter


def test_init_with_agent_name_sets_the_resource_attribute(init_env, no_metadata):
    redberry_sdk.init(agent="support-bot")
    (provider,) = init_env
    attrs = provider.resource.attributes
    assert attrs["redberry.agent.name"] == "support-bot"
    assert "redberry.agent_id" not in attrs  # ingest stamps it
    assert attrs["service.name"] == "redberry-agent-support-bot"
    assert _otlp(provider)._endpoint == "http://localhost:4318/v1/traces"


def test_init_with_agent_on_cloud_run(init_env, metadata):
    metadata(token=_jwt(9_999_999_999))
    redberry_sdk.init(agent="support-bot")
    (provider,) = init_env
    otlp = _otlp(provider)
    assert otlp._endpoint == "https://redberry-ingest-123456.europe-west2.run.app/v1/traces"


def test_init_rejects_a_bad_name_before_anything(init_env):
    with pytest.raises(ValueError, match="agent"):
        redberry_sdk.init(agent="bad\nname")
    assert init_env == []


def test_init_legacy_agent_id_unchanged(init_env, metadata):
    server = metadata()
    redberry_sdk.init(agent_id="legacy")
    (provider,) = init_env
    attrs = provider.resource.attributes
    assert attrs["redberry.agent_id"] == "legacy" and "redberry.agent.name" not in attrs
    assert _otlp(provider)._endpoint == "http://localhost:4318/v1/traces"
    assert server.requests == []  # no discovery for an unnamed agent


def test_init_bundle_env_unchanged(init_env, metadata, monkeypatch):
    metadata()
    monkeypatch.setenv("OTELLINE_OTEL_ENDPOINT", "http://collector:4318")
    redberry_sdk.init(agent="bot", agent_id="a1")
    (provider,) = init_env
    assert _otlp(provider)._endpoint == "http://collector:4318/v1/traces"
    assert provider.resource.attributes["redberry.agent_id"] == "a1"


def test_init_requires_a_name_or_an_id(init_env):
    with pytest.raises(ValueError, match="agent or agent_id"):
        redberry_sdk.init()


def test_init_uses_no_hasher(init_env, no_metadata, monkeypatch):
    import sys
    for name in [m for m in sys.modules if m.startswith("redberry_sdk.hasher")]:
        monkeypatch.delitem(sys.modules, name)
    redberry_sdk.init(agent="bot")
    assert not any(m.startswith("redberry_sdk.hasher") for m in sys.modules)


# ── 401/403 while a new grant propagates ─────────────────────────────────────────────────────────

@pytest.fixture
def fast_backoff(monkeypatch):
    monkeypatch.setattr(_emit, "AUTH_RETRY_FIRST_S", 0.01)
    monkeypatch.setattr(_emit, "AUTH_RETRY_MAX_S", 0.05)


def _refusing_ingest(status: int, refusals: int):
    """A fake ingest that answers ``status`` to the first ``refusals`` posts, then 200."""
    count = [0]

    def route(_handler):
        count[0] += 1
        if count[0] <= refusals:
            return (status, "", {})
        return (200, "", {"Content-Type": "application/x-protobuf"})
    return _Server({"/v1/traces": route})


def _run(endpoint, n_spans=3):
    processor = _emit.build_processor(endpoint)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("t")
    for _ in range(n_spans):
        tracer.start_span("s").end()
    provider.force_flush()
    provider.shutdown()


def test_403_until_the_grant_propagates_then_delivered(no_metadata, fast_backoff):
    ingest = _refusing_ingest(403, 3)
    try:
        _run(_emit.Endpoint(f"http://{ingest.host}", "explicit"))
        assert len([p for p, _ in ingest.requests if p == "/v1/traces"]) == 4
        assert redberry_sdk.dropped_spans() == 0
    finally:
        ingest.close()


def test_401_refetches_the_token(metadata, fast_backoff):
    md = metadata(token=_jwt(9_999_999_999))
    ingest = _refusing_ingest(401, 2)
    try:
        _run(_emit.Endpoint(f"http://{ingest.host}", "gcp", "https://redberry-ingest-123456.r.run.app"))
        assert redberry_sdk.dropped_spans() == 0
        assert len([p for p, _ in md.requests if "identity" in p]) == 3  # first + one per 401
    finally:
        ingest.close()


def test_budget_exhausted_counts_once_and_names_the_grant(metadata, fast_backoff, monkeypatch, caplog):
    md = metadata(token=_jwt(9_999_999_999))
    md.routes[MD + "instance/service-accounts/default/email"] = (200, "agent@p.iam.gserviceaccount.com", G)
    monkeypatch.setenv(_emit.AUTH_RETRY_ENV, "0.2")
    ingest = _refusing_ingest(403, 10**6)
    try:
        with caplog.at_level("WARNING", logger="redberry.sdk"):
            _run(_emit.Endpoint(f"http://{ingest.host}", "gcp", "https://a"), n_spans=3)
        assert redberry_sdk.dropped_spans() == 3
        assert len([p for p, _ in ingest.requests if p == "/v1/traces"]) > 2
        refused = [r.message for r in caplog.records if "refused" in r.message]
        assert len(refused) == 1
        assert "(403)" in refused[0] and "roles/run.invoker" in refused[0]
        assert "agent@p.iam.gserviceaccount.com" in refused[0] and "step 13" in refused[0]
    finally:
        ingest.close()


def test_shutdown_during_retry_returns_promptly(no_metadata, monkeypatch):
    import time
    monkeypatch.setattr(_emit, "AUTH_RETRY_FIRST_S", 30.0)
    ingest = _refusing_ingest(403, 10**6)
    try:
        processor = _emit.build_processor(_emit.Endpoint(f"http://{ingest.host}", "explicit"))
        provider = TracerProvider()
        provider.add_span_processor(processor)
        provider.get_tracer("t").start_span("s").end()
        threading.Thread(target=processor.force_flush, args=(60_000,), daemon=True).start()
        deadline = time.monotonic() + 5
        while not ingest.requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ingest.requests  # the worker is now waiting out the 403
        started = time.monotonic()
        provider.shutdown()
        assert time.monotonic() - started < 2
        assert redberry_sdk.dropped_spans() == 1
    finally:
        ingest.close()


def test_force_flush_bounds_the_retry(no_metadata, monkeypatch):
    import time
    monkeypatch.setattr(_emit, "AUTH_RETRY_FIRST_S", 30.0)
    ingest = _refusing_ingest(403, 10**6)
    try:
        processor = _emit.build_processor(_emit.Endpoint(f"http://{ingest.host}", "explicit"))
        provider = TracerProvider()
        provider.add_span_processor(processor)
        provider.get_tracer("t").start_span("s").end()
        started = time.monotonic()
        processor.force_flush(500)
        assert time.monotonic() - started < 3
        provider.shutdown()
    finally:
        ingest.close()


def test_other_failures_are_not_retried(no_metadata, fast_backoff):
    ingest = _refusing_ingest(400, 10**6)
    try:
        _run(_emit.Endpoint(f"http://{ingest.host}", "explicit"), n_spans=1)
        assert len([p for p, _ in ingest.requests if p == "/v1/traces"]) == 1
        assert redberry_sdk.dropped_spans() == 1
    finally:
        ingest.close()


def test_auth_retry_budget_env(monkeypatch):
    monkeypatch.setenv(_emit.AUTH_RETRY_ENV, "12")
    assert _emit.auth_retry_budget() == 12.0
    monkeypatch.setenv(_emit.AUTH_RETRY_ENV, "soon")
    assert _emit.auth_retry_budget() == _emit.AUTH_RETRY_DEFAULT_S
    monkeypatch.delenv(_emit.AUTH_RETRY_ENV)
    assert _emit.auth_retry_budget() == _emit.AUTH_RETRY_DEFAULT_S


# ── Cloud Run resource attributes ────────────────────────────────────────────────────────────────

def test_cloud_run_resource_attributes(init_env, metadata, monkeypatch):
    metadata(token=_jwt(9_999_999_999))
    monkeypatch.setenv("K_SERVICE", "support-bot-svc")
    monkeypatch.setenv("K_REVISION", "support-bot-svc-00007-abc")
    redberry_sdk.init(agent="support-bot")
    (provider,) = init_env
    attrs = provider.resource.attributes
    assert attrs["cloud.provider"] == "gcp"
    assert attrs["cloud.platform"] == "gcp_cloud_run"
    assert attrs["faas.name"] == "support-bot-svc"
    assert attrs["faas.version"] == "support-bot-svc-00007-abc"
    assert attrs["cloud.region"] == "europe-west2"
    assert attrs["cloud.account.id"] == "acme-prod"


def test_cloud_run_attributes_absent_off_cloud_run(init_env, metadata):
    metadata(token=_jwt(9_999_999_999))
    redberry_sdk.init(agent="support-bot")
    (provider,) = init_env
    attrs = provider.resource.attributes
    assert not any(k.startswith(("cloud.", "faas.")) for k in attrs)


def test_cloud_run_attributes_without_metadata_never_fail(init_env, no_metadata, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "svc")
    redberry_sdk.init(agent="support-bot")
    (provider,) = init_env
    attrs = provider.resource.attributes
    assert attrs["faas.name"] == "svc" and attrs["cloud.platform"] == "gcp_cloud_run"
    assert not {"faas.version", "cloud.region", "cloud.account.id"} & set(attrs)


def test_explicit_resource_attributes_win_over_cloud_run(init_env, no_metadata, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "svc")
    redberry_sdk.init(agent="support-bot", extra_resource_attributes={"faas.name": "mine"})
    (provider,) = init_env
    assert provider.resource.attributes["faas.name"] == "mine"
