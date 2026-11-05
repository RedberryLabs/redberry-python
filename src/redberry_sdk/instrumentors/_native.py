"""Native OTel instrumentors — the primary capture path.

Each class wraps one upstream ``Instrumentor().instrument()`` call. Content
capture is enabled globally via the OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT
environment variable (set by sdk.init before these run), so individual
instrumentors don't need to be told explicitly.

Supported instrumentors (in priority order):
  OpenAI          opentelemetry-instrumentation-openai-v2  (official)
  Anthropic       openinference-instrumentation-anthropic   (best available)
  LangChain       opentelemetry-instrumentation-langchain   (opentelemetry-python-contrib)
  OpenAI Agents   opentelemetry-instrumentation-openai-agents-v2 (official, when released)
"""

from __future__ import annotations

import importlib
import importlib.util

from ._base import BaseInstrumentor


class OpenAIInstrumentor(BaseInstrumentor):
    name = "OpenAI"
    library = "openai"

    def is_available(self) -> bool:
        return importlib.util.find_spec("openai") is not None

    def enable(self, logger_provider=None) -> None:
        from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor as _I
        kwargs = {}
        if logger_provider is not None:
            kwargs["logger_provider"] = logger_provider
        _I().instrument(**kwargs)


class AnthropicInstrumentor(BaseInstrumentor):
    name = "Anthropic"
    library = "anthropic"

    def is_available(self) -> bool:
        return importlib.util.find_spec("anthropic") is not None

    def enable(self, logger_provider=None) -> None:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor as _I
        _I().instrument()


class LangChainInstrumentor(BaseInstrumentor):
    name = "LangChain"
    library = "langchain"

    def is_available(self) -> bool:
        return importlib.util.find_spec("langchain") is not None

    def enable(self, logger_provider=None) -> None:
        # Try official opentelemetry-instrumentation-langchain first,
        # fall back to openinference variant.
        try:
            from opentelemetry.instrumentation.langchain import LangchainInstrumentor as _I
            _I().instrument()
        except ImportError:
            from openinference.instrumentation.langchain import LangChainInstrumentor as _I  # type: ignore[no-redef]
            _I().instrument()


class OpenAIAgentsInstrumentor(BaseInstrumentor):
    """OpenAI Agents SDK instrumentor (openai-agents / openai-agents-sdk)."""

    name = "OpenAI Agents SDK"
    library = "agents"  # openai-agents package

    def is_available(self) -> bool:
        # By distribution, not import name: a project's own `agents/` package is not the
        # OpenAI Agents SDK.
        from ._expected import EXPECTED, library_present
        return any(library_present(e) for e in EXPECTED if e.library == self.library)

    def enable(self, logger_provider=None) -> None:
        # The official opentelemetry-instrumentation-openai-agents-v2 may not yet
        # be released; fall back to openinference variant.
        try:
            from opentelemetry.instrumentation.openai_agents_v2 import OpenAIAgentsInstrumentor as _I
            _I().instrument()
        except ImportError:
            from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor as _I  # type: ignore[no-redef]
            _I().instrument()


# Ordered list — the SDK iterates this and calls try_enable() on each.
ALL_NATIVE_INSTRUMENTORS: list[BaseInstrumentor] = [
    OpenAIInstrumentor(),
    AnthropicInstrumentor(),
    LangChainInstrumentor(),
    OpenAIAgentsInstrumentor(),
]
