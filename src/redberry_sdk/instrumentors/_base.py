"""Base class for OTel instrumentor wrappers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class InstrumentorStatus:
    name: str
    library: str                # importable package name
    enabled: bool
    reason: str                 # why it was or wasn't enabled
    capture_tier: str           # "full", "partial_content", "metadata_only"


class BaseInstrumentor(ABC):
    """Wraps an upstream OTel instrumentor (native or wrapt-based fallback).

    Subclasses implement ``is_available()`` (can the library be imported?) and
    ``enable()`` (call the upstream instrumentor's ``.instrument()`` method with
    content-capture enabled).
    """

    name: str
    library: str

    @abstractmethod
    def is_available(self) -> bool:
        """True if the target library is importable in this environment."""

    @abstractmethod
    def enable(self, logger_provider: Any = None) -> None:
        """Activate instrumentation. Called only when is_available() is True."""

    def try_enable(self, logger_provider: Any = None) -> InstrumentorStatus:
        if not self.is_available():
            return InstrumentorStatus(
                name=self.name,
                library=self.library,
                enabled=False,
                reason="library not installed",
                capture_tier="none",
            )
        try:
            self.enable(logger_provider=logger_provider)
            return InstrumentorStatus(
                name=self.name,
                library=self.library,
                enabled=True,
                reason="native OTel instrumentor activated",
                capture_tier="full",
            )
        except Exception as exc:  # noqa: BLE001
            return InstrumentorStatus(
                name=self.name,
                library=self.library,
                enabled=False,
                reason=_missing_reason(self.library, exc) or f"enable() raised: {exc}",
                capture_tier="none",
            )


def _missing_reason(library: str, exc: Exception) -> str | None:
    """The target library imported but its instrumentation package did not: say which extra to
    install, rather than the generic "enable() raised". ``init()`` refuses to start on this case
    (see ``_expected``), so this reason is what the coverage report shows under the opt-out."""
    if not isinstance(exc, ImportError):
        return None
    from ._expected import EXPECTED, install_hint

    for e in EXPECTED:
        if e.library == library:
            return f"instrumentation package missing: {install_hint(e.extra)}"
    return None
