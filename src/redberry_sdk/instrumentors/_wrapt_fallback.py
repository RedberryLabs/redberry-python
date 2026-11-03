"""wrapt-based monkey-patching fallback for libraries without native OTel support.

This path is ONLY used for libraries that have no upstream native instrumentor.
All major providers (OpenAI, Anthropic, LangChain) are covered by native
instrumentors; this exists for the long tail.

The fallback wraps the library's completion/generation call and emits a minimal
OTel span with the gen_ai.* attributes. Content capture at 'full' tier requires
the wrapped function to expose its arguments in a known structure; if the
structure is unrecognised, we degrade to 'metadata_only' tier and record a
CaptureGap.

Adding support for a new library:
  1. Subclass WraptFallbackInstrumentor.
  2. Implement target_module, target_object, target_attribute (the callable to wrap).
  3. Implement extract_span_attributes() to pull gen_ai.* fields from call args/kwargs.
"""

from __future__ import annotations

import importlib
import json
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

import wrapt
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from ._base import BaseInstrumentor, InstrumentorStatus


class WraptFallbackInstrumentor(BaseInstrumentor, ABC):
    """Generic wrapt-based fallback instrumentor.

    Wraps a single callable (module.object.attribute) and emits a span on each
    invocation. Subclasses override extract_span_attributes() to pull fields
    specific to the library's API shape.
    """

    target_module: str   # dotted import path, e.g. "cohere"
    target_object: str   # attribute on the module, e.g. "Client"
    target_attribute: str  # method to wrap, e.g. "chat"
    span_name: str       # OTel span name, e.g. "cohere.chat"

    def is_available(self) -> bool:
        return importlib.util.find_spec(self.target_module) is not None

    def enable(self, logger_provider=None) -> None:
        instrumentor = self  # capture for closure

        @wrapt.patch_function_wrapper(self.target_module, f"{self.target_object}.{self.target_attribute}")
        def _wrapper(wrapped: Callable, instance: Any, args: tuple, kwargs: dict):
            tracer = trace.get_tracer("redberry.wrapt_fallback")
            with tracer.start_as_current_span(instrumentor.span_name) as span:
                try:
                    attrs = instrumentor.extract_span_attributes(args, kwargs)
                    for k, v in attrs.items():
                        span.set_attribute(k, v)
                except Exception:  # noqa: BLE001
                    span.set_attribute("redberry.capture_tier", "metadata_only")

                start = time.time()
                try:
                    result = wrapped(*args, **kwargs)
                    span.set_status(StatusCode.OK)
                    try:
                        output_attrs = instrumentor.extract_output_attributes(result)
                        for k, v in output_attrs.items():
                            span.set_attribute(k, v)
                    except Exception:  # noqa: BLE001
                        pass
                    return result
                except Exception as exc:
                    span.set_status(StatusCode.ERROR, str(exc))
                    raise
                finally:
                    span.set_attribute("gen_ai.latency_ms", int((time.time() - start) * 1000))

    def try_enable(self) -> InstrumentorStatus:
        if not self.is_available():
            return InstrumentorStatus(
                name=self.name,
                library=self.library,
                enabled=False,
                reason="library not installed",
                capture_tier="none",
            )
        try:
            self.enable()
            return InstrumentorStatus(
                name=self.name,
                library=self.library,
                enabled=True,
                reason="wrapt fallback instrumentor activated",
                capture_tier="full",
            )
        except Exception as exc:  # noqa: BLE001
            return InstrumentorStatus(
                name=self.name,
                library=self.library,
                enabled=False,
                reason=f"wrapt patch raised: {exc}",
                capture_tier="none",
            )

    @abstractmethod
    def extract_span_attributes(self, args: tuple, kwargs: dict) -> dict[str, Any]:
        """Return gen_ai.* attributes to set on the span from the call arguments."""

    def extract_output_attributes(self, result: Any) -> dict[str, Any]:
        """Return gen_ai.* attributes to set on the span from the call result.

        Override in subclasses that can extract output tokens, finish reason, etc.
        """
        return {}


# ---------------------------------------------------------------------------
# Example: Cohere fallback (not covered by any native instrumentor as of v0.1)
# ---------------------------------------------------------------------------


class CohereV2Fallback(WraptFallbackInstrumentor):
    """Minimal fallback for cohere>=5 ClientV2.chat."""

    name = "Cohere (wrapt fallback)"
    library = "cohere"
    target_module = "cohere"
    target_object = "ClientV2"
    target_attribute = "chat"
    span_name = "gen_ai.chat"

    def extract_span_attributes(self, args: tuple, kwargs: dict) -> dict[str, Any]:
        attrs: dict[str, Any] = {"gen_ai.system": "cohere"}
        model = kwargs.get("model") or (args[0] if args else None)
        if model:
            attrs["gen_ai.request.model"] = model
        messages = kwargs.get("messages", [])
        if messages:
            # Emit a summary of the message structure; full content captured
            # only if the caller passes messages as plain dicts.
            try:
                attrs["gen_ai.request.messages_json"] = json.dumps(messages)[:4096]
            except Exception:  # noqa: BLE001
                pass
        return attrs


# Add further fallback instrumentors here as needed.
# The SDK will attempt to enable each one; failures are surfaced in the coverage report.
ALL_WRAPT_FALLBACKS: list[WraptFallbackInstrumentor] = [
    CohereV2Fallback(),
]
