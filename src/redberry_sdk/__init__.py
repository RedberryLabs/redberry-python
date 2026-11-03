"""Redberry SDK: records an AI agent's model and tool calls and sends them to the customer's Redberry install.

Usage::

    import redberry_sdk
    redberry_sdk.init(agent="my-agent")

After init(), model calls made through supported libraries (OpenAI, Anthropic, LangChain and others) are
recorded with OpenTelemetry and sent to the ingest service of the Redberry install in the customer's own cloud
(Google Cloud, AWS or Azure), or to an explicit endpoint.
"""

from ._sdk import ContentCaptureDisabledError, MissingInstrumentationError, flush, flush_after, init, get_tracer, shutdown
from ._call_role import internal_call
from ._emit import dropped_spans
from ._config import SDKConfig, EvaluationTopology
from ._coverage import CoverageReport
from ._version import __version__

__all__ = ["__version__", "init", "dropped_spans", "flush", "flush_after", "get_tracer", "shutdown", "internal_call", "MissingInstrumentationError", "ContentCaptureDisabledError", "SDKConfig", "EvaluationTopology", "CoverageReport"]
