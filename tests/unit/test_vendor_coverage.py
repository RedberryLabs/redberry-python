"""Every LLM vendor we claim to capture must actually be reachable.

This exists because of a specific mistake: Gemini was assumed to be covered
because `opentelemetry-instrumentation-google-generativeai` is a declared
dependency. That package instruments the **deprecated** `google.generativeai`
SDK. Google's current SDK is `google-genai` (`google.genai`) — a different
package, with a different entry point, which nothing here covered. An agent
built against the current SDK would have run and produced no LLM spans at all,
and the traces would have looked merely sparse rather than broken.

So: assert the wiring, not the intention. A vendor is covered if it is either
in the curated `ALL_NATIVE_INSTRUMENTORS` tier or discoverable via a tier-2
entry point that discovery would not skip.
"""
from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from redberry_sdk.instrumentors import (
    _ALLOWED_DIST_PREFIXES,
    _DISCOVERY_SKIP_LIBRARIES,
)
from redberry_sdk.instrumentors._native import ALL_NATIVE_INSTRUMENTORS

#: (human name, entry-point/library name) for every vendor a RAP reference
#: agent may be built against. Add a row here BEFORE adding the agent.
REQUIRED_VENDORS = [
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Google Gen AI (current SDK, google.genai)", "google_genai"),
    ("Google Generative AI (legacy SDK)", "google_generativeai"),
]


def _curated_libraries() -> set[str]:
    return {i.library for i in ALL_NATIVE_INSTRUMENTORS}


def _discoverable_libraries() -> set[str]:
    """Entry points tier-2 discovery would actually activate."""
    out = set()
    for ep in entry_points(group="opentelemetry_instrumentor"):
        dist = ep.dist.name if ep.dist else ""
        if not dist.startswith(_ALLOWED_DIST_PREFIXES):
            continue
        if ep.name in _DISCOVERY_SKIP_LIBRARIES:
            continue
        out.add(ep.name)
    return out


@pytest.mark.parametrize("label,library", REQUIRED_VENDORS,
                         ids=[v[1] for v in REQUIRED_VENDORS])
def test_vendor_is_reachable(label: str, library: str) -> None:
    curated, discoverable = _curated_libraries(), _discoverable_libraries()
    assert library in curated or library in discoverable, (
        f"{label} is not instrumentable: {library!r} is in neither the curated "
        f"tier ({sorted(curated)}) nor tier-2 discovery ({sorted(discoverable)}). "
        f"An agent built on it would emit no LLM spans."
    )


def test_curated_libraries_are_skipped_by_discovery() -> None:
    """A curated instrumentor exists to control `.instrument()` kwargs (e.g.
    logger_provider for OpenAI v2 message content). If discovery also activates
    that library, the curated kwargs are bypassed or it is instrumented twice."""
    for library in _curated_libraries():
        if library in {ep.name for ep in entry_points(group="opentelemetry_instrumentor")}:
            assert library in _DISCOVERY_SKIP_LIBRARIES, (
                f"{library!r} has both a curated instrumentor and a live entry "
                f"point, but is not in _DISCOVERY_SKIP_LIBRARIES"
            )
