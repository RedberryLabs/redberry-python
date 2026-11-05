"""SDK core — bootstraps OTel, enables instrumentors, emits coverage report."""

from __future__ import annotations

import logging
import os
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

from ._config import SDKConfig
from ._coverage import CoverageReport
from . import _emit
from ._span_event_logger import SpanEventLoggerProvider
from .instrumentors import activate_all
from .instrumentors._base import InstrumentorStatus
from .instrumentors._expected import failed_to_enable, install_hint, missing_instrumentation
from . import valp
from ._call_role import CallRoleSpanProcessor
from .valp import ToolCatalogue, ValpSequencingExporter, ValpSpanProcessor, ValpToolRanMarker
from . import _env

_log = logging.getLogger("redberry.sdk")


class MissingInstrumentationError(RuntimeError):
    """A supported library is installed without its instrumentation package (see
    ``instrumentors/_expected.py``): the agent would run unobserved."""


class ContentCaptureDisabledError(RuntimeError):
    """Content capture is off, and nothing said it should be: the evidence would hold no prompts or
    completions, and pricing could not score the agent."""


_CAPTURE_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"


def _check_content_capture(config: Any, stacklevel: int = 3) -> None:
    """Refuse a start with content capture off unless it was chosen (``allow_content_capture_off``).

    Off means ``capture_content=False``, or the instrumentors' own switch inherited from the
    environment as anything but ``"true"`` (it used to win silently: ``setdefault`` left it).
    Allowed, the run is metadata-only and says so loudly."""
    inherited = os.environ.get(_CAPTURE_ENV)
    if config.capture_content and (inherited is None or inherited.strip().lower() == "true"):
        return
    why = "capture_content=False" if not config.capture_content else f"{_CAPTURE_ENV}={inherited!r} in the environment"
    if not config.allow_content_capture_off:
        raise ContentCaptureDisabledError(
            f"content capture is off ({why}): the evidence and pricing both need the prompts and completions. "
            "Remove the setting, or set allow_content_capture_off / REDBERRY_ALLOW_CONTENT_CAPTURE_OFF=1 to "
            "run metadata-only deliberately.")
    message = f"redberry_sdk: running WITHOUT content capture ({why}), as allowed: metadata only"
    warnings.warn(message, RuntimeWarning, stacklevel=stacklevel)
    _log.error(message)


def prepare_instrumentation(*, capture_content: bool = True, allow_missing_instrumentation: bool = False,
                            allow_content_capture_off: bool = False) -> None:
    """Steps 1b, 1c and 2 of ``init()``, shared with ``redberry_sdk.hasher.register()``.

    1b. Refuse to run unobserved. A library that imports while its instrumentation package does not
        (a venv rebuilt with a plain `pip install -e .` after the core/extras split) would otherwise
        run normally and ship traces with no LLM spans.
    1c. Refuse to run with content capture off unless that was chosen.
    2.  Set the environment variable the instrumentors read at instrument() time.
    """
    missing = missing_instrumentation()
    if missing:
        detail = "; ".join(f"{e.library}: {e.detail} ({install_hint(e.extra)})" for e in missing)
        if not allow_missing_instrumentation:
            raise MissingInstrumentationError(
                f"installed without working instrumentation, so they would not be observed: {detail}. "
                "Install the extras, or set allow_missing_instrumentation / "
                "REDBERRY_ALLOW_MISSING_INSTRUMENTATION=1 to run without them.")
        message = f"redberry_sdk: running WITHOUT instrumentation for {detail}"
        warnings.warn(message, RuntimeWarning, stacklevel=3)
        _log.error(message)

    _check_content_capture(SimpleNamespace(capture_content=capture_content,
                                           allow_content_capture_off=allow_content_capture_off), stacklevel=4)

    # Must happen BEFORE calling Instrumentor().instrument().
    if capture_content:
        os.environ.setdefault(_CAPTURE_ENV, "true")
    else:
        os.environ[_CAPTURE_ENV] = "false"


def enable_instrumentation(*, allow_missing_instrumentation: bool = False) -> list[InstrumentorStatus]:
    """Steps 3b, 4 and 4b of ``init()``, shared with ``redberry_sdk.hasher.register()``.

    3b. Install the span-event bridge as the global LoggerProvider: opentelemetry-instrumentation-openai-v2
        >= 2.x emits message content via Logger.emit() (OTel Events API) rather than span.add_event(),
        and the bridge re-emits those records as span events so the normaliser's span-event parser
        picks them up.
    4.  Enable the instrumentors.
    4b. Refuse to run unobserved, part two: an instrumentation that loads (1b passed) but raises when
        enabled instruments nothing. This is after setup, so the process is half instrumented when it
        raises; the error is meant to stop it.
    """
    logger_provider = SpanEventLoggerProvider()
    set_logger_provider(logger_provider)
    statuses = activate_all(logger_provider=logger_provider)
    failed = failed_to_enable(statuses)
    if failed:
        detail = "; ".join(f"{e.library}: {e.detail}" for e in failed)
        if not allow_missing_instrumentation:
            raise MissingInstrumentationError(
                f"installed with instrumentation that failed to start, so they would not be observed: {detail}. Check the "
                "installed OpenTelemetry versions against the extras' pins, or set allow_missing_instrumentation / "
                "REDBERRY_ALLOW_MISSING_INSTRUMENTATION=1 to run without them.")
        message = f"redberry_sdk: running WITHOUT instrumentation for {detail}"
        warnings.warn(message, RuntimeWarning, stacklevel=3)
        _log.error(message)
    return statuses


_INITIALIZED = False
_TRACER_PROVIDER: TracerProvider | None = None
_last_coverage_report: CoverageReport | None = None



def _load_otelline_dotenv() -> None:
    """`redberry connect` writes the agent's REDBERRY_AGENT_ID into its repository's .env. When the agent's
    process hasn't loaded that file itself, read it here: from the working directory upwards, else beside
    the running script. Only REDBERRY_* (and the earlier OTELLINE_*) keys are taken, and never over a value already in the
    environment, so nothing else in that file (an API key, say) is set behind the agent's back."""
    if _env.get("REDBERRY_AGENT_ID"):
        return
    try:
        from dotenv import dotenv_values, find_dotenv
    except ImportError:
        return
    import sys as _sys

    candidates = [find_dotenv(usecwd=True)]
    main = getattr(_sys.modules.get("__main__"), "__file__", None)
    if main:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(main)), ".env"))
    for path in candidates:
        if path and os.path.isfile(path):
            for key, value in (dotenv_values(path) or {}).items():
                if key.startswith(("REDBERRY_", "OTELLINE_")) and value and key not in os.environ:
                    os.environ[key] = value
            if _env.get("REDBERRY_AGENT_ID"):
                return

def init(
    agent_id: str | None = None,
    *,
    agent: str | None = None,
    config: SDKConfig | None = None,
    config_path: str | Path | None = None,
    span_processors: list[SpanProcessor] | None = None,
    **kwargs: Any,
) -> CoverageReport:
    """Bootstrap the Redberry observation layer.

    Call once at agent startup, before any LLM calls. Idempotent — subsequent
    calls return the existing coverage report without re-instrumenting.

    Parameters
    ----------
    agent_id:
        Stable identifier for this agent across runs. Required if `config` is
        not provided. May also be passed as the first positional argument.
    agent:
        The agent's name, chosen by the customer: 1 to 64 printable characters, no leading or
        trailing spaces. Sent on every span as the resource attribute ``redberry.agent.name``;
        the Redberry installation's ingest maps it to the agent id it generates, so ``agent_id``
        is then optional. Naming the agent also turns on endpoint discovery on GCP (see
        ``redberry_sdk._emit``): with no ``REDBERRY_OTEL_ENDPOINT``, spans go to the installation's
        ``redberry-ingest`` service with an ID token.
    config:
        Pre-built SDKConfig. Overrides config_path and all kwargs.
    config_path:
        Path to redberry.yaml. Loaded and merged with any kwargs.
    span_processors:
        Extra span processors, added to the TracerProvider before the OTLP exporter. The
        ``valp.*`` processor (redberry_sdk.valp.ValpSpanProcessor) is always installed first.
    **kwargs:
        Any field from SDKConfig can be passed as a keyword argument and will
        override the file config (or serve as the sole config when no file is
        given).
    """
    global _INITIALIZED, _TRACER_PROVIDER, _last_coverage_report

    if _INITIALIZED:
        return _last_coverage_report  # type: ignore[return-value]
    if agent is not None:
        agent = _emit.validate_agent_name(agent)

    # ------------------------------------------------------------------
    # 1. Resolve config
    # ------------------------------------------------------------------
    _load_otelline_dotenv()
    if config is None:
        if config_path is not None:
            config = SDKConfig.from_yaml(config_path)
        else:
            _id = agent_id or kwargs.pop("agent_id", None)
            if _id is None:
                # Allow agent_id to come from REDBERRY_AGENT_ID env var (set
                # by a test harness). apply_env_overrides() will fill it
                # in below; default to empty string here so dataclass init
                # succeeds.
                _id = _env.get("REDBERRY_AGENT_ID") or ""
            config = SDKConfig(agent_id=_id, **kwargs)
    elif agent_id is not None:
        config.agent_id = agent_id

    # Pull REDBERRY_* (or the earlier OTELLINE_*) env vars over any unset fields. Explicit kwargs / YAML
    # win; env fills the rest.
    config.apply_env_overrides()
    if not config.agent_id and agent is None:
        raise ValueError(
            "agent or agent_id is required (pass to init() or set REDBERRY_AGENT_ID)"
        )
    # The name stands in where the id is only a label (service.name, the coverage report).
    label = config.agent_id or agent

    # ------------------------------------------------------------------
    # 1b, 1c, 2. Refuse to run unobserved or without content, and set what the instrumentors read
    #     (prepare_instrumentation). Before anything is set up, so a refused start leaves no
    #     half-instrumented process.
    # ------------------------------------------------------------------
    prepare_instrumentation(
        capture_content=config.capture_content,
        allow_missing_instrumentation=config.allow_missing_instrumentation,
        allow_content_capture_off=config.allow_content_capture_off,
    )

    # ------------------------------------------------------------------
    # 3. Build TracerProvider with OTLP HTTP exporter pointing at the Collector
    # ------------------------------------------------------------------
    resource_attrs = {"service.name": f"redberry-agent-{label}"}
    # Where the agent runs (Cloud Run): the scoring job reads faas.name to find the container image.
    resource_attrs.update(_emit.cloud_run_resource_attributes())
    # Without an id, ingest stamps redberry.agent_id from the name.
    if config.agent_id:
        resource_attrs["redberry.agent_id"] = config.agent_id
    if agent is not None:
        resource_attrs[_emit.AGENT_NAME_ATTRIBUTE] = agent
    if config.run_id:
        resource_attrs["redberry.run_id"] = config.run_id
    if config.task_class:
        resource_attrs["redberry.task_class"] = config.task_class
    # The agent's code version and manifest hash, for VALP spec v2's observed fingerprint.
    resource_attrs.update(config.fingerprint_attributes())
    resource_attrs.update(config.extra_resource_attributes)

    resource = Resource(attributes=resource_attrs)
    provider = TracerProvider(resource=resource)

    # valp.* first, so its on_start values are on the span before anything else sees it.
    valp.configure(pending_approval_ttl_s=config.valp_pending_approval_ttl_s)
    catalogue = ToolCatalogue.load(config.valp_tool_catalogue) if config.valp_tool_catalogue else None
    provider.add_span_processor(ValpSpanProcessor(catalogue))
    # redberry.call_role on the spans of calls the agent makes for itself
    # (redberry_sdk.internal_call), so the normaliser does not read them as replies.
    provider.add_span_processor(CallRoleSpanProcessor())
    provider.add_span_processor(_emit.DiscoveryFailureMarker())
    for processor in span_processors or ():
        provider.add_span_processor(processor)

    # Every exported span is numbered per emitter (valp.emitter_id / valp.span_seq), so VALP can
    # tell a span lost between here and its sink from one that never existed. Outside the
    # numbering, a tool span the SDK opened for a call that never ran (paused for approval, or
    # denied) is marked valp.tool_ran = false, not dropped: VALP stores it and reads it as OTHER,
    # never as a tool call; the normaliser keeps it too (redberry_sdk.valp.ValpToolRanMarker).
    # The emit path (redberry_sdk._emit): REDBERRY_OTEL_ENDPOINT / an explicit endpoint, else the
    # GCP ingest service (named agents only, with an ID token), else the local collector; a bounded
    # queue that never blocks the agent, and a count of whatever is dropped. No hashing, signing,
    # keys or storage writes on this path.
    endpoint = _emit.resolve_endpoint(config.otel_endpoint, discover=agent is not None)
    if endpoint.source in ("gcp", "aws"):
        _log.info("redberry_sdk: sending spans to %s", endpoint.url)
    config.otel_endpoint = endpoint.url
    provider.add_span_processor(_emit.build_processor(
        endpoint, wrap=lambda otlp: ValpToolRanMarker(ValpSequencingExporter(otlp))))

    trace.set_tracer_provider(provider)
    _TRACER_PROVIDER = provider

    # ------------------------------------------------------------------
    # 3b, 4, 4b. The span-event bridge and the instrumentors (enable_instrumentation).
    # ------------------------------------------------------------------
    statuses = enable_instrumentation(allow_missing_instrumentation=config.allow_missing_instrumentation)

    # ------------------------------------------------------------------
    # 5. Coverage report
    # ------------------------------------------------------------------
    report = CoverageReport.build(agent_id=label, statuses=statuses)
    report.print_summary()
    report.persist()

    _INITIALIZED = True
    _last_coverage_report = report

    return report


def get_tracer(name: str = "redberry.user") -> trace.Tracer:
    """Return a tracer from the Redberry provider. Call after init()."""
    if not _INITIALIZED:
        raise RuntimeError("redberry.init() has not been called.")
    return trace.get_tracer(name)


def flush(timeout_s: float = 30.0) -> bool:
    """Send every span recorded so far, now; True when they all went. On a platform that freezes the process
    between requests (AWS Lambda, Cloud Run with CPU throttling) call it before the handler returns, or use
    ``flush_after``. A no-op before ``init()``. Never raises."""
    provider = _TRACER_PROVIDER
    if provider is None:
        return True
    try:
        return bool(provider.force_flush(int(timeout_s * 1000)))
    except Exception:  # noqa: BLE001 - flushing must never fail the agent
        return False


def flush_after(handler):
    """Decorate a request handler (e.g. a Lambda ``handler(event, context)``) so its spans are sent before it
    returns, including when it raises::

        @redberry_sdk.flush_after
        def handler(event, context): ...
    """
    import functools

    @functools.wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        finally:
            flush()

    return wrapped


def shutdown(exit_status: str = "ok") -> None:
    """Flush and shut down the tracer provider. Call at agent process exit.

    ``exit_status`` is accepted for compatibility and is currently unused.
    """
    global _INITIALIZED, _TRACER_PROVIDER
    # Close any approval-paused interaction whose TTL has run out, before the spans are flushed.
    agents_integration = sys.modules.get("redberry_sdk.integrations.openai_agents")
    if agents_integration is not None:
        agents_integration.sweep()
    if _TRACER_PROVIDER is not None:
        _TRACER_PROVIDER.shutdown()
    _INITIALIZED = False
    _TRACER_PROVIDER = None
