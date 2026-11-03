"""Environment settings.

Each setting is read as ``REDBERRY_<NAME>``. The earlier ``OTELLINE_<NAME>`` spelling is still accepted, so
existing deployments keep working; when both are set, ``REDBERRY_<NAME>`` wins.
"""

from __future__ import annotations

import os

_PREFIX = "REDBERRY_"
_LEGACY_PREFIX = "OTELLINE_"


def get(name: str) -> str | None:
    """The value of ``name`` (a ``REDBERRY_*`` name), falling back to its ``OTELLINE_*`` spelling."""
    value = os.environ.get(name)
    if value is None and name.startswith(_PREFIX):
        value = os.environ.get(_LEGACY_PREFIX + name[len(_PREFIX):])
    return value
