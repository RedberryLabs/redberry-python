"""Which instrumentation each supported library needs, and which extra installs it.

After the core/extras split, an environment can have a library (``openai``) without its
instrumentation package (``opentelemetry-instrumentation-openai-v2``): a venv rebuilt with a plain
``pip install -e .`` is enough. The agent then runs normally and ships traces with no LLM spans in
them, and the only sign is a line in the coverage report. ``init()`` therefore checks this table
BEFORE instrumenting anything and refuses to start unless told otherwise
(``SDKConfig.allow_missing_instrumentation``): an agent everyone believes is observed but isn't is
worse than one that won't start.

Deliberately not listed:

- ``cohere``: the wrapt fallback (``_wrapt_fallback.CohereV2Fallback``) covers it when the
  Traceloop instrumentation is absent, so it is never unobserved.
- ``boto3`` (Bedrock): boto3 is a general AWS client. Its presence says nothing about Bedrock use,
  so requiring the Bedrock instrumentation for every boto3 user would refuse healthy agents.

Two ways the check used to be wrong, both fixed here:

- **An instrumentor that is installed but does not import passed it.** Only import *specs* were
  consulted, so ``opentelemetry-instrumentation-openai-v2`` resolved against an
  ``opentelemetry-util-genai`` it cannot import was "present": ``init()`` started and the agent ran
  with no LLM spans. An instrumentation module now counts only if it actually imports (importing an
  instrumentor patches nothing; ``.instrument()`` does that), and one that fails is reported with
  its error.
- **A library was detected by import name alone.** The OpenAI Agents SDK's import name is
  ``agents``, which is also what any project calls its own ``agents/`` package (RAP's, run from its
  root: system_map §8.12). Where an import name is that generic, ``Expected.distributions`` names
  the distribution that must provide it, and a same-named module from anywhere else is not the
  library.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Expected", "EXPECTED", "Missing", "missing_instrumentation", "install_hint",
           "library_present"]


@dataclass(frozen=True)
class Expected:
    library: str                 # import name of the target library
    modules: tuple[str, ...]     # instrumentation modules; any one of them is enough
    extra: str                   # the redberry-agent-sdk extra that installs one
    # The distribution(s) that must provide ``library`` for it to count. Empty = the import name is
    # distinctive enough on its own. Set where it is not (``agents``).
    distributions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Missing:
    """A library that is present while none of its instrumentation modules loads."""

    library: str
    extra: str
    modules: tuple[str, ...]
    #: Why: the package is absent, or it is installed and its import fails (with the error).
    detail: str


EXPECTED: tuple[Expected, ...] = (
    Expected("openai", ("opentelemetry.instrumentation.openai_v2",), "providers"),
    Expected("anthropic", ("openinference.instrumentation.anthropic",), "providers"),
    Expected("mistralai", ("opentelemetry.instrumentation.mistralai",), "providers"),
    Expected("ollama", ("opentelemetry.instrumentation.ollama",), "providers"),
    Expected("vertexai", ("opentelemetry.instrumentation.vertexai",), "providers"),
    Expected("google.generativeai", ("opentelemetry.instrumentation.google_generativeai",), "providers"),
    Expected("google.genai", ("openinference.instrumentation.google_genai",), "providers"),
    Expected("groq", ("opentelemetry.instrumentation.groq",), "providers"),
    Expected("together", ("opentelemetry.instrumentation.together",), "providers"),
    Expected("replicate", ("opentelemetry.instrumentation.replicate",), "providers"),
    Expected("langchain", ("opentelemetry.instrumentation.langchain", "openinference.instrumentation.langchain"),
             "frameworks"),
    Expected("agents", ("opentelemetry.instrumentation.openai_agents_v2", "openinference.instrumentation.openai_agents"),
             "frameworks", distributions=("openai-agents",)),
    Expected("llama_index", ("opentelemetry.instrumentation.llamaindex",), "frameworks"),
    Expected("crewai", ("opentelemetry.instrumentation.crewai",), "frameworks"),
    Expected("haystack", ("opentelemetry.instrumentation.haystack",), "frameworks"),
    Expected("chromadb", ("opentelemetry.instrumentation.chromadb",), "vectordb"),
    Expected("pinecone", ("opentelemetry.instrumentation.pinecone",), "vectordb"),
    Expected("qdrant_client", ("opentelemetry.instrumentation.qdrant",), "vectordb"),
    Expected("weaviate", ("opentelemetry.instrumentation.weaviate",), "vectordb"),
)


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # a missing parent package ("google" for "google.genai")
        return False


def install_hint(extra: str) -> str:
    return f"pip install 'redberry-agent-sdk[{extra}]'"


def _import_error(name: str) -> str | None:
    """None if ``name`` imports; otherwise the error, as ``Type: message``."""
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 — anything that stops the import is a failure to load
        return f"{type(exc).__name__}: {exc}"
    return None


def _provided_by(library: str, distributions: tuple[str, ...]) -> bool:
    """Whether the module ``library`` resolves to belongs to one of ``distributions``.

    The spec's origin must be one of the distribution's own files. A distribution installed in
    editable mode lists no source files (only a .pth or finder), so for one that lists no file
    under the package at all, being installed is taken as enough.
    """
    try:
        spec = importlib.util.find_spec(library)
    except (ImportError, ValueError):
        return False
    origin = spec.origin if spec is not None else None
    if spec is not None and not origin and spec.submodule_search_locations:
        origin = next(iter(spec.submodule_search_locations), None)
    for dist_name in distributions:
        try:
            dist = importlib.metadata.distribution(dist_name)
        except importlib.metadata.PackageNotFoundError:
            continue
        files = [f for f in (dist.files or []) if f.parts and f.parts[0] == library.split(".")[0]]
        if not files:
            return True
        if origin is None:
            continue
        try:
            target = Path(origin).resolve()
            for f in files:
                p = Path(str(dist.locate_file(f))).resolve()
                if p == target or target in p.parents or p in target.parents:
                    return True
        except OSError:
            continue
    return False


def library_present(e: Expected) -> bool:
    """The library ``e`` describes is importable here, and is that library, not a namesake."""
    if not _importable(e.library):
        return False
    return not e.distributions or _provided_by(e.library, e.distributions)


def _instrumentation_problem(e: Expected) -> str | None:
    """None if one of ``e.modules`` loads; otherwise why none does."""
    failures = []
    for m in e.modules:
        if not _importable(m):
            continue
        err = _import_error(m)
        if err is None:
            return None
        failures.append(f"{m} is installed but fails to import ({err})")
    if failures:
        return "; ".join(failures)
    return "instrumentation package missing"


def missing_instrumentation(skip: frozenset[str] = frozenset()) -> list[Missing]:
    """Libraries present in this environment while none of their instrumentation modules loads.

    ``skip`` names libraries covered another way (e.g. ``agents`` when the VALP integration for the
    OpenAI Agents SDK is in use). The target libraries are never imported; an instrumentation
    module is, because a module that is found but cannot be imported instruments nothing.
    Importing an instrumentor does not patch anything.
    """
    out: list[Missing] = []
    for e in EXPECTED:
        if e.library in skip or not library_present(e):
            continue
        problem = _instrumentation_problem(e)
        if problem is not None:
            out.append(Missing(e.library, e.extra, e.modules, problem))
    return out


def failed_to_enable(statuses: list) -> list[Missing]:
    """Libraries present whose instrumentation loads but raised when enabled, and that nothing else
    instrumented.

    ``missing_instrumentation`` cannot see this: the module imports, then ``instrument()`` fails, and
    the library runs unobserved. An OpenTelemetry release did exactly that to openai-v2 (the span
    event bridge became abstract), and ``init()`` reported success while OpenAI calls left no spans.
    """
    enabled = {s.library for s in statuses if s.enabled}
    out: list[Missing] = []
    for e in EXPECTED:
        if e.library in enabled or not library_present(e):
            continue
        raised = [s.reason for s in statuses if s.library == e.library and s.reason.startswith("enable() raised")]
        if raised:
            out.append(Missing(e.library, e.extra, e.modules, raised[0]))
    return out
