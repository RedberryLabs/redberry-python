"""Instrumentor activation.

Three-tier pipeline:

1. **Curated native instrumentors** (``ALL_NATIVE_INSTRUMENTORS``) — hand-picked
   classes for libraries where we need control over the ``.instrument()`` kwargs
   (e.g. passing ``logger_provider=`` to the OpenAI v2 instrumentor so that
   message-content events flow through the SpanEventLoggerProvider bridge).

2. **Entry-point discovery** — walks the ``opentelemetry_instrumentor`` entry
   point group and activates any instrumentor whose distribution name matches
   the allow-list prefix set. This is how OpenLLMetry / Traceloop packages
   (opentelemetry-instrumentation-bedrock, -cohere, -mistralai, …), Google's
   current Gen AI SDK (openinference-instrumentation-google-genai) and any
   future OTel-compliant instrumentor come online with zero code changes.

3. **Wrapt fallbacks** (``ALL_WRAPT_FALLBACKS``) — last-resort monkey-patching
   for libraries that ship no OTel instrumentor at all.

Each tier dedupes against the libraries already covered by earlier tiers, so
no library is double-instrumented even if it's reachable from multiple paths.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from ._base import BaseInstrumentor, InstrumentorStatus  # noqa: F401 (re-exported)
from ._expected import install_hint, missing_instrumentation
from ._native import ALL_NATIVE_INSTRUMENTORS
from ._request_snapshot import REQUEST_EVENT, RESPONSE_EVENT
from ._tool_definitions import TOOLS_EVENT, enable_tool_definition_capture
from ._wrapt_fallback import ALL_WRAPT_FALLBACKS


# Only auto-activate instrumentors from these distributions. Keeps stray
# OTel instrumentors (e.g. a user's own opentelemetry-instrumentation-requests)
# from being activated by surprise.
_ALLOWED_DIST_PREFIXES: tuple[str, ...] = (
    "opentelemetry-instrumentation-",
    "openinference-instrumentation-",
)

# Entry-point names we never auto-activate via discovery. Either already handled
# by the curated tier (preserving special kwargs like logger_provider), or
# intentionally excluded because we prefer a different implementation.
_DISCOVERY_SKIP_LIBRARIES: frozenset[str] = frozenset(
    {
        # Covered by curated ALL_NATIVE_INSTRUMENTORS with special kwargs
        "openai",
        "anthropic",
        "langchain",
        # General-purpose OTel instrumentors that aren't agent-observation relevant.
        # Users who want HTTP/DB instrumentation should wire those up themselves.
        "requests",
        "urllib",
        "urllib3",
        "httpx",
        "aiohttp_client",
        "aiohttp_server",
        "sqlalchemy",
        "psycopg2",
        "psycopg",
        "pymongo",
        "redis",
        "boto",
        "botocore",
        "grpc_client",
        "grpc_server",
        "logging",
        "system_metrics",
        "threading",
        "asyncio",
        "wsgi",
        "asgi",
        "fastapi",
        "starlette",
        "flask",
        "django",
        "tornado",
    }
)


def _discovered_entry_points() -> list[Any]:
    """Return allow-listed opentelemetry_instrumentor entry points."""
    try:
        eps = list(entry_points(group="opentelemetry_instrumentor"))
    except TypeError:  # Python < 3.10 fallback (not expected given pyproject pin)
        eps = list(entry_points().get("opentelemetry_instrumentor", []))

    out: list[Any] = []
    for ep in eps:
        dist_name = (getattr(ep, "dist", None) and ep.dist.name) or ""
        if not any(dist_name.startswith(p) for p in _ALLOWED_DIST_PREFIXES):
            continue
        if ep.name in _DISCOVERY_SKIP_LIBRARIES:
            continue
        out.append(ep)
    return out


def _activate_entry_point(ep: Any) -> InstrumentorStatus:
    """Load and instrument one discovered entry point. Never raises."""
    dist_name = (getattr(ep, "dist", None) and ep.dist.name) or "unknown"
    library = ep.name
    pretty_name = f"{library} ({dist_name})"
    try:
        cls = ep.load()
    except Exception as exc:  # noqa: BLE001
        return InstrumentorStatus(
            name=pretty_name,
            library=library,
            enabled=False,
            reason=f"entry-point load failed: {exc}",
            capture_tier="none",
        )

    try:
        instance = cls()
    except Exception as exc:  # noqa: BLE001
        return InstrumentorStatus(
            name=pretty_name,
            library=library,
            enabled=False,
            reason=f"instantiation failed: {exc}",
            capture_tier="none",
        )

    # If the instrumentor advertises its target library dependency, honour it:
    # skip when the target isn't importable to avoid noisy activation errors.
    deps = getattr(instance, "instrumentation_dependencies", None)
    if callable(deps):
        try:
            import importlib.util
            import re

            for spec in deps() or []:
                # spec is e.g. "openai >= 1.0" — extract the bare module name
                pkg = re.split(r"[<>=!~\s]", spec, maxsplit=1)[0].strip()
                if pkg and importlib.util.find_spec(pkg) is None:
                    return InstrumentorStatus(
                        name=pretty_name,
                        library=library,
                        enabled=False,
                        reason=f"target library '{pkg}' not installed",
                        capture_tier="none",
                    )
        except Exception:  # noqa: BLE001 — dependency check is best-effort
            pass

    try:
        instance.instrument()
    except Exception as exc:  # noqa: BLE001
        return InstrumentorStatus(
            name=pretty_name,
            library=library,
            enabled=False,
            reason=f"instrument() raised: {exc}",
            capture_tier="none",
        )

    return InstrumentorStatus(
        name=pretty_name,
        library=library,
        enabled=True,
        reason="auto-discovered via opentelemetry_instrumentor entry point",
        capture_tier="full",
    )


def activate_all(logger_provider: Any = None) -> list[InstrumentorStatus]:
    """Enable every instrumentor available in this environment.

    Pipeline: curated → entry-point discovery → wrapt fallbacks. Each stage
    dedupes against libraries already enabled by earlier stages.
    """
    statuses: list[InstrumentorStatus] = []

    # Tier 0 — tool DEFINITIONS, before anything else.
    #
    # No upstream instrumentor records the tools a request was made with — only
    # the one the model called. wrapt wrappers nest in application order and the
    # last applied is outermost, so this has to go on FIRST to end up inside the
    # LLM span the native instrumentor opens. Applied after, the event would
    # attach to the wrong span or to none.
    patched = enable_tool_definition_capture()
    statuses.append(InstrumentorStatus(
        name="ToolDefinitions", library="(supplement)", enabled=bool(patched),
        reason=(f"recording {TOOLS_EVENT}, {REQUEST_EVENT} and {RESPONSE_EVENT} "
                f"on {len(patched)} call site(s)"
                if patched else "no supported client installed"),
        capture_tier="full" if patched else "none"))

    # Tier 1 — curated native instrumentors (special kwargs preserved)
    for inst in ALL_NATIVE_INSTRUMENTORS:
        statuses.append(inst.try_enable(logger_provider=logger_provider))

    # Tier 2 — entry-point discovery for everything else
    enabled_libs = {s.library for s in statuses if s.enabled}
    for ep in _discovered_entry_points():
        if ep.name in enabled_libs:
            continue
        status = _activate_entry_point(ep)
        statuses.append(status)
        if status.enabled:
            enabled_libs.add(status.library)

    # Tier 3 — wrapt monkey-patching fallbacks
    for inst in ALL_WRAPT_FALLBACKS:
        if inst.library in enabled_libs:
            continue
        status = inst.try_enable()
        statuses.append(status)
        if status.enabled:
            enabled_libs.add(status.library)

    # A library that imports while its instrumentation package does not. The curated tier reports
    # its own (see _base._missing_reason); a discovery-tier package that is absent has no entry
    # point to walk, so without this it would simply not appear in the report.
    reported = {s.library for s in statuses}
    for e in missing_instrumentation():
        if e.library not in reported and e.library not in enabled_libs:
            statuses.append(InstrumentorStatus(
                name=e.library, library=e.library, enabled=False,
                reason=_reason(e), capture_tier="none"))

    return statuses


def _reason(e: Any) -> str:
    detail = getattr(e, "detail", None)
    if detail and detail != "instrumentation package missing":
        return f"instrumentation fails to import: {detail}; {install_hint(e.extra)}"
    return f"instrumentation package missing: {install_hint(e.extra)}"
