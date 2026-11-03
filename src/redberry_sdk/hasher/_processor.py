"""``RedberryProcessor``: an OpenTelemetry span processor that, per trace (one invocation), collects the
spans, decides when the run is over, and then, with instrumentation suppressed::

    canonical JSON -> gzip -> SHA-256 -> store.put -> signed hash report -> pricing sample

The port of ``@redberrylabs/otel``'s processor (SPEC.md §8). **Finish rule:** the local root span has
ended, no span of the trace is open, and nothing new started for ``grace_ms``. **Escape hatches** cut a
trace short and still store and report it: ``hard_stop_ms`` after the root ended, the function deadline
(``deadline_ms``), and ``shutdown()``. A span that starts or ends after finalisation is counted as late
and ignored; it never produces a second document.

Outside Vercel there is no ``waitUntil``: work started by a timer runs on a background thread, which a
host may freeze once the invocation returns. ``with_redberry`` therefore finalises its invocation's trace
itself, through ``complete_trace``, and waits for the store and the report before the handler returns.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from ._agents import is_agent_id, resolve_agent
from ._config import HasherConfig, resolve_config
from ._crypto import Signer, load_signer
from ._envelope import (
    AGENT_ATTRIBUTE,
    CutReason,
    Finish,
    build_envelope,
    encode_trace,
    storage_path,
)
from ._report import (
    ReportOutcome,
    SampleOutcome,
    report_body,
    send_report,
    upload_sample,
)
from ._span import is_ai_span, parent_span_id, span_id_hex, span_to_json, trace_id_hex
from .stores import PutMeta, StoreConflictError

__all__ = ["RedberryProcessor", "TraceResult"]

log = logging.getLogger("redberry.hasher")
_REMEMBERED = 10_000

_COUNTERS = ("traces", "complete", "cut_short", "skipped_no_ai_spans", "skipped_no_agent", "dropped_traces",
             "dropped_spans", "late_spans", "stored", "store_failed", "recorded", "duplicates", "refused",
             "report_failed", "samples", "samples_failed")


@dataclass
class TraceResult:
    trace_id: str
    finish: Finish
    cut_reason: CutReason | None
    #: Set when the trace was not hashed: no model or agent span, or no agent for it.
    skipped: str | None = None
    agent_id: str | None = None
    span_count: int | None = None
    sha256: str | None = None
    size: int | None = None
    path: str | None = None
    store: str | None = None
    stored: bool | None = None
    store_error: str | None = None
    report: ReportOutcome | None = None
    sample: SampleOutcome | None = None
    #: The stored bytes (gzip of the canonical JSON).
    data: bytes | None = field(default=None, repr=False)


@dataclass
class _Trace:
    trace_id: str
    ended: list[ReadableSpan] = field(default_factory=list)
    open: dict[str, Span] = field(default_factory=dict)
    started: set[str] = field(default_factory=set)
    root_id: str | None = None
    root: Span | None = None
    root_ended: bool = False
    first_start_ms: float = 0.0
    dropped_spans: int = 0
    agent: str | None = None
    timers: dict[str, threading.Timer] = field(default_factory=dict)


class _Work:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: TraceResult | None = None


def _remember(memory: OrderedDict[str, None], key: str) -> None:
    memory[key] = None
    if len(memory) > _REMEMBERED:
        memory.popitem(last=False)


class RedberryProcessor(SpanProcessor):
    """Options are ``resolve_config``'s keywords (``agent_id``, ``agents_by_route``, ``store``, ``url``,
    ``token``, ``signing_key``, ``key_id``, ``grace_ms``, ``hard_stop_ms``, ``deadline_ms``, ...); anything
    not given comes from the environment. Pass ``config`` to use an already resolved ``HasherConfig``."""

    def __init__(self, config: HasherConfig | None = None, **options: Any) -> None:
        self.cfg = config if config is not None else resolve_config(**options)
        self._lock = threading.Condition(threading.RLock())
        self._traces: dict[str, _Trace] = {}
        self._finalised: OrderedDict[str, None] = OrderedDict()
        self._ignored: OrderedDict[str, None] = OrderedDict()
        self._work: dict[str, _Work] = {}
        self._signer: Signer | None = None
        self._counts = dict.fromkeys(_COUNTERS, 0)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def stats(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {"enabled": self.cfg.enabled, "open_traces": len(self._traces), **self._counts}
        if self.cfg.disabled_reason:
            out["disabled_reason"] = self.cfg.disabled_reason
        return out

    # ── SpanProcessor ─────────────────────────────────────────────────────────────────────────────

    def on_start(self, span: Span, parent_context: otel_context.Context | None = None) -> None:
        if not self.cfg.enabled:
            return
        sc = span.get_span_context()
        tid, sid = trace_id_hex(sc.trace_id), span_id_hex(sc.span_id)
        with self._lock:
            if tid in self._ignored:
                return
            if tid in self._finalised:
                self._counts["late_spans"] += 1
                log.debug("[redberry] span %r started after trace %s was finalised", span.name, tid)
                return
            t = self._traces.get(tid)
            if t is None:
                if len(self._traces) >= self.cfg.max_traces:
                    self._counts["dropped_traces"] += 1
                    _remember(self._ignored, tid)
                    return
                t = self._open(tid)
            if len(t.started) >= self.cfg.max_spans_per_trace:
                t.dropped_spans += 1
                self._counts["dropped_spans"] += 1
                return
            parent = parent_span_id(span)
            if t.root_id is None and (parent is None or parent not in t.started):
                t.root_id, t.root = sid, span
            t.started.add(sid)
            t.open[sid] = span
            self._cancel(t, "grace")

    def on_end(self, span: ReadableSpan) -> None:
        if not self.cfg.enabled:
            return
        sc = span.get_span_context()
        tid, sid = trace_id_hex(sc.trace_id), span_id_hex(sc.span_id)
        with self._lock:
            if tid in self._finalised:
                self._counts["late_spans"] += 1
                return
            t = self._traces.get(tid)
            if t is None or sid not in t.open:
                return  # not ours: ignored, dropped, or started before this processor was added
            del t.open[sid]
            t.ended.append(span)
            if sid == t.root_id:
                t.root_ended = True
                self._timer(t, "hard_stop", self.cfg.hard_stop_ms, lambda: self._finalise(tid, "hard_stop"))
            if t.root_ended and not t.open:
                self._timer(t, "grace", self.cfg.grace_ms, lambda: self._grace_over(tid))
            self._lock.notify_all()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Waits for traces already being stored and reported; it does not cut open traces short."""
        deadline = time.monotonic() + timeout_millis / 1000
        with self._lock:
            works = list(self._work.values())
        return all(w.done.wait(max(0.0, deadline - time.monotonic())) for w in works)

    def shutdown(self) -> None:
        """Cuts every open trace short (cut_reason "shutdown"), then waits for all of them."""
        with self._lock:
            tids = list(self._traces)
        for tid in tids:
            self._finalise(tid, "shutdown")
        self.force_flush()

    # ── one invocation (with_redberry) ────────────────────────────────────────────────────────────

    def root_span_id(self, trace_id: str) -> str | None:
        with self._lock:
            t = self._traces.get(trace_id)
            return t.root_id if t is not None else None

    def complete_trace(self, trace_id: str, *, agent_id: str | None = None,
                       timeout: float | None = None) -> TraceResult | None:
        """Finalise ``trace_id`` now that its root has ended, and wait (at most ``timeout`` seconds) for its
        store, report and sample. Spans still open are given ``grace_ms`` to end; any still open then are
        included open, and the trace is cut short (``hard_stop``). ``agent_id`` overrides the configured
        agent for this trace. None when the trace is not tracked here (disabled, not sampled) or its work
        did not finish within ``timeout``."""
        if not self.cfg.enabled:
            return None
        with self._lock:
            t = self._traces.get(trace_id)
            if t is not None:
                if agent_id:
                    t.agent = agent_id
                wait_until = time.monotonic() + self.cfg.grace_ms / 1000
                while t.open and trace_id in self._traces and (left := wait_until - time.monotonic()) > 0:
                    self._lock.wait(left)
            if trace_id in self._traces:
                work = self._finalise(trace_id, None if (t.root_ended and not t.open) else "hard_stop")
            else:
                work = self._work.get(trace_id)  # a timer finalised it meanwhile
        if work is None:
            return None
        return work.result if work.done.wait(timeout) else None

    # ── per trace ─────────────────────────────────────────────────────────────────────────────────

    def _open(self, tid: str) -> _Trace:
        t = _Trace(tid, first_start_ms=time.time() * 1000)
        self._traces[tid] = t
        self._counts["traces"] += 1
        if self.cfg.deadline_ms is not None:
            at = t.first_start_ms + self.cfg.deadline_ms - self.cfg.deadline_margin_ms
            self._timer(t, "deadline", max(0.0, at - time.time() * 1000), lambda: self._finalise(tid, "deadline"))
        return t

    def _timer(self, t: _Trace, name: str, ms: float, fn: Any) -> None:
        self._cancel(t, name)
        timer = threading.Timer(ms / 1000, fn)
        timer.daemon = True
        t.timers[name] = timer
        timer.start()

    @staticmethod
    def _cancel(t: _Trace, name: str) -> None:
        timer = t.timers.pop(name, None)
        if timer is not None:
            timer.cancel()

    def _grace_over(self, tid: str) -> None:
        with self._lock:
            t = self._traces.get(tid)
            if t is not None and not t.open:
                self._finalise(tid, None)

    def _finalise(self, tid: str, reason: CutReason | None) -> _Work | None:
        """``reason`` None: the finish rule held. Otherwise an escape hatch fired."""
        with self._lock:
            t = self._traces.pop(tid, None)
            if t is None:
                return None
            _remember(self._finalised, tid)
            for name in list(t.timers):
                self._cancel(t, name)
            finish: Finish = "complete" if t.root_ended and not t.open else "cut_short"
            cut_at_ms = time.time_ns() // 1_000_000
            self._counts["complete" if finish == "complete" else "cut_short"] += 1
            ended, still_open = list(t.ended), list(t.open.values())
            work = _Work()
            self._work[tid] = work
        cut_reason = None if finish == "complete" else (reason or "hard_stop")
        thread = threading.Thread(target=self._run, args=(t, ended, still_open, finish, cut_reason, cut_at_ms, work),
                                  name=f"redberry-hasher-{tid[:8]}", daemon=True)
        thread.start()
        return work

    def _run(self, t: _Trace, ended: list[ReadableSpan], still_open: list[Span], finish: Finish,
             cut_reason: CutReason | None, cut_at_ms: int, work: _Work) -> None:
        token = otel_context.attach(otel_context.set_value(otel_context._SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            work.result = self._process(t, ended, still_open, finish, cut_reason, cut_at_ms)
            if self.cfg.on_result is not None:
                self.cfg.on_result(work.result)
        except Exception as e:  # noqa: BLE001 - never into the customer's code
            log.error("[redberry] trace %s: %s", t.trace_id, e)
        finally:
            otel_context.detach(token)
            with self._lock:
                self._work.pop(t.trace_id, None)
            work.done.set()

    def _count(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def _process(self, t: _Trace, ended: list[ReadableSpan], still_open: list[Span], finish: Finish,
                 cut_reason: CutReason | None, cut_at_ms: int) -> TraceResult:
        result = TraceResult(t.trace_id, finish, cut_reason)
        spans = [span_to_json(s) for s in ended] + [span_to_json(s, open=True) for s in still_open]
        result.span_count = len(spans)
        if self.cfg.require_ai_spans and not any(is_ai_span(s) for s in spans):
            self._count("skipped_no_ai_spans")
            result.skipped = "no_ai_spans"
            return result
        root_attrs = dict(t.root.attributes or {}) if t.root is not None else {}
        stamped = root_attrs.get(AGENT_ATTRIBUTE)
        agent_id = t.agent or (stamped if is_agent_id(stamped) else None) or resolve_agent(
            root_attrs, self.cfg.agents_by_route, self.cfg.agent_id)
        if not agent_id or not is_agent_id(agent_id):
            self._count("skipped_no_agent")
            route = root_attrs.get("http.route") or root_attrs.get("url.path") or (t.root.name if t.root else "?")
            log.warning("[redberry] trace %s: no agent for %s; not hashed", t.trace_id, route)
            result.skipped = "no_agent"
            return result
        first = t.root if t.root is not None else (ended[0] if ended else None)
        resource = dict(first.resource.attributes) if first is not None and first.resource is not None else {}
        envelope = build_envelope(agent_id=agent_id, trace_id=t.trace_id, finish=finish, cut_reason=cut_reason,
                                  cut_at_ms=cut_at_ms if finish == "cut_short" else None, resource=resource,
                                  spans=spans, dropped_spans=t.dropped_spans)
        encoded = encode_trace(envelope)
        store = self.cfg.store
        assert store is not None  # enabled implies a store
        result.agent_id, result.sha256, result.size = agent_id, encoded.sha256, encoded.size
        result.path, result.store, result.data = storage_path(envelope), store.kind, encoded.gz

        try:
            store.put(result.path, encoded.gz, PutMeta(agent_id, t.trace_id, encoded.sha256, encoded.size))
            result.stored = True
            self._count("stored")
        except Exception as e:  # noqa: BLE001 - the report goes out with stored: false
            result.stored = False
            result.store_error = str(e) if isinstance(e, StoreConflictError) else f"{type(e).__name__}: {e}"
            self._count("store_failed")
            log.error("[redberry] trace %s: not stored (%s): %s", t.trace_id, store.kind, result.store_error)

        body = report_body(agent_id=agent_id, trace_id=t.trace_id, sha256=encoded.sha256, size=encoded.size,
                           started_at=envelope["started_at"], ended_at=envelope["ended_at"],
                           stored=result.stored is True, store=store.kind)
        try:
            signer = self._load_signer()
        except Exception as e:  # noqa: BLE001
            self._count("report_failed")
            result.report = ReportOutcome("failed", None, 0, error=f"signing key: {e}")
            log.error("[redberry] trace %s: not reported: %s", t.trace_id, result.report.error)
            return result
        report = send_report(url=self.cfg.url, token=self.cfg.token, signer=signer, body=body,
                             transport=self.cfg.transport, retry_delays=self.cfg.retry_delays)
        result.report = report
        if report.outcome == "recorded":
            self._count("recorded")
        elif report.outcome == "duplicate":
            self._count("duplicates")
        else:
            self._count("refused" if report.outcome == "refused" else "report_failed")
            log.error("[redberry] trace %s: hash report %s after %d attempt(s): %s", t.trace_id, report.outcome,
                      report.attempts, report.error)

        if report.outcome in ("recorded", "duplicate") and report.sample is not None:
            result.sample = upload_sample(report.sample, encoded.gz, self.cfg.transport, self.cfg.retry_delays)
            if result.sample.ok:
                self._count("samples")
            else:
                self._count("samples_failed")
                log.warning("[redberry] trace %s: pricing sample not uploaded: %s", t.trace_id, result.sample.error)
        log.debug("[redberry] trace %s %s%s agent=%s spans=%d sha256=%s stored=%s report=%s", t.trace_id, finish,
                  f"/{cut_reason}" if cut_reason else "", agent_id, len(spans), encoded.sha256[:16], result.stored,
                  report.outcome)
        return result

    def _load_signer(self) -> Signer:
        with self._lock:
            if self._signer is None:
                self._signer = load_signer(self.cfg.signing_key, self.cfg.key_id)
            return self._signer
