"""Sampling the provider fixes, recorded where the request cannot state it.

Pricing scores a model call only if it sampled at temperature exactly 1.0, and
it reads the temperature from the request. Anthropic's API version
``2026-10-21`` rejects ``temperature``, ``top_p`` and ``top_k`` on every model,
even ``temperature: 1``: sampling is fixed at the defaults, temperature 1.0. The
``anthropic`` Python SDK sends that version by default from 2.0.0. An agent on a
current SDK therefore samples at exactly 1.0 and has no way to say so in the
request, and without this every such capture would be refused as "temperature
not recorded".

So on each Anthropic ``Messages`` call the SDK records, as attributes of the
model-call span:

- ``redberry.llm.provider_api_version``: the ``anthropic-version`` the call used;
- ``redberry.llm.provider_api_version_source``: how that was learnt, strongest
  first:

  * ``request_header``: the header the call sends, read the way the client
    builds it: the call's ``extra_headers``, else the client's
    ``default_headers`` (which include any ``default_headers=`` override);
  * ``sdk_default``: the installed SDK's own default, when the client's headers
    cannot be read;

- on ``2026-10-21`` or later, when the request sent no temperature,
  ``redberry.llm.temperature_inferred = 1.0`` and
  ``redberry.llm.temperature_source = "provider_fixed_default"``.

The normaliser carries them onto the event's decoding parameters, and pricing
keeps the source tag in the evidence, so an underwriter can see the temperature
was inferred from the provider's rule and not read from the request.

What is never inferred
----------------------
- **Calls on ``2023-06-01``.** Anthropic's default there is also 1.0, but an
  omitted temperature cannot be told from an explicit one the instrumentation
  failed to record. Such calls must still send and record 1.0.
- **Any route other than the first-party API**: only an ``anthropic.Anthropic``
  or ``AsyncAnthropic`` client (not a subclass: ``AnthropicAWS`` is one) whose
  base URL is ``api.anthropic.com``. Bedrock and Vertex carry the version in the
  body (``anthropic_version``), and Foundry, gateways and proxies each need
  their own check first.
- **Over a temperature the request sent.** That is recorded as sent.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from opentelemetry import trace

logger = logging.getLogger(__name__)

#: Span attributes the Redberry install reads.
ATTR_API_VERSION = "redberry.llm.provider_api_version"
ATTR_API_VERSION_SOURCE = "redberry.llm.provider_api_version_source"
ATTR_TEMPERATURE_INFERRED = "redberry.llm.temperature_inferred"
ATTR_TEMPERATURE_SOURCE = "redberry.llm.temperature_source"

PROVIDER_FIXED_DEFAULT = "provider_fixed_default"
FROM_REQUEST_HEADER = "request_header"
FROM_SDK_DEFAULT = "sdk_default"

#: The first Anthropic API version on which sampling is fixed at the defaults.
#: Versions are ISO dates, so they order as strings.
ANTHROPIC_FIXED_SAMPLING_FROM = "2026-10-21"
#: The temperature Anthropic samples at when none may be sent.
ANTHROPIC_DEFAULT_TEMPERATURE = 1.0

_FIRST_PARTY_CLIENTS = frozenset({"Anthropic", "AsyncAnthropic"})
_FIRST_PARTY_HOST = "api.anthropic.com"
_VERSION_HEADER = "anthropic-version"
#: Request parameters that are sampling parameters the 2026-10-21 API rejects.
_SAMPLING_KEYS = ("temperature", "top_p", "top_k")


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def fixes_sampling(version: object) -> bool:
    """An ISO-date API version on or after ``2026-10-21``. Not a bare string comparison:
    ``"beta" >= "2026-10-21"`` is true."""
    return (isinstance(version, str) and _ISO_DATE.fullmatch(version) is not None
            and version >= ANTHROPIC_FIXED_SAMPLING_FROM)


def _header(headers: Any) -> tuple[bool, str | None]:
    """(whether ``headers`` sets ``anthropic-version``, its value). An SDK
    ``Omit`` (the header removed) is set, to None: the call sends no version."""
    if not isinstance(headers, Mapping):
        return False, None
    for k, v in headers.items():
        if str(k).lower() == _VERSION_HEADER:
            if isinstance(v, str):
                return True, v
            return True, None
    return False, None


def _first_party(client: Any) -> bool:
    """An ``anthropic.Anthropic`` / ``AsyncAnthropic`` (exactly) calling api.anthropic.com."""
    cls = type(client)
    if cls.__name__ not in _FIRST_PARTY_CLIENTS or not cls.__module__.startswith("anthropic"):
        return False
    base_url = getattr(client, "base_url", None)
    host = getattr(base_url, "host", None)
    if host is None and isinstance(base_url, str):
        from urllib.parse import urlparse
        host = urlparse(base_url).hostname
    return host == _FIRST_PARTY_HOST


def _sdk_default_version() -> str | None:
    """The ``anthropic-version`` the installed SDK sends unless told otherwise."""
    try:
        from anthropic._constants import ANTHROPIC_API_VERSION
        if isinstance(ANTHROPIC_API_VERSION, str):
            return ANTHROPIC_API_VERSION
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from importlib.metadata import version
        major = int(version("anthropic").split(".")[0])
    except Exception:                                    # noqa: BLE001
        return None
    # 2.0.0 moved the default from 2023-06-01 to 2026-10-21.
    return ANTHROPIC_FIXED_SAMPLING_FROM if major >= 2 else "2023-06-01"


def anthropic_api_version(instance: Any, kwargs: dict) -> tuple[str | None, str | None]:
    """(the ``anthropic-version`` this call uses, how that was learnt), or
    (None, None) when it is not a first-party call or cannot be known.

    ``instance`` is the ``Messages`` resource; its ``_client`` builds every
    request's headers from its ``default_headers`` merged with the call's
    ``extra_headers``, the call's winning.
    """
    client = getattr(instance, "_client", None)
    if client is None or not _first_party(client):
        return None, None
    found, value = _header(kwargs.get("extra_headers"))
    if found:
        return (value, FROM_REQUEST_HEADER) if value else (None, None)
    try:
        defaults = client.default_headers
    except Exception:                                    # noqa: BLE001
        defaults = None
    found, value = _header(defaults)
    if found:
        return (value, FROM_REQUEST_HEADER) if value else (None, None)
    # The client's headers could not be read: a client-level override
    # (`default_headers=`) still wins over the SDK's default.
    found, value = _header(getattr(client, "_custom_headers", None))
    if found:
        return (value, FROM_REQUEST_HEADER) if value else (None, None)
    version = _sdk_default_version()
    return (version, FROM_SDK_DEFAULT) if version else (None, None)


def _sent(kwargs: dict, key: str) -> bool:
    """Whether the request carries ``key``, as a parameter or in ``extra_body``."""
    def given(v: Any) -> bool:
        return v is not None and type(v).__name__ not in ("NotGiven", "Omit")
    if given(kwargs.get(key)):
        return True
    extra = kwargs.get("extra_body")
    return isinstance(extra, Mapping) and given(extra.get(key))


def provider_default_attributes(instance: Any, kwargs: dict) -> dict[str, Any]:
    """The span attributes for one Anthropic ``Messages`` call. Pure; exposed for tests."""
    version, how = anthropic_api_version(instance, kwargs)
    if version is None:
        return {}
    attributes: dict[str, Any] = {ATTR_API_VERSION: version, ATTR_API_VERSION_SOURCE: how}
    if fixes_sampling(version) and not _sent(kwargs, "temperature"):
        attributes[ATTR_TEMPERATURE_INFERRED] = ANTHROPIC_DEFAULT_TEMPERATURE
        attributes[ATTR_TEMPERATURE_SOURCE] = PROVIDER_FIXED_DEFAULT
    return attributes


def record_provider_defaults_on_current_span(instance: Any, kwargs: dict) -> bool:
    """Put this call's provider facts on the model-call span in progress.

    Returns whether anything was recorded. Never raises into the caller's call:
    a missing fact is a capture gap (the capture is refused, as before), not
    the agent's error.
    """
    from ._request_snapshot import _is_llm_span

    span = trace.get_current_span()
    if span is None or not span.is_recording() or not _is_llm_span(span):
        return False
    attributes = provider_default_attributes(instance, kwargs)
    if not attributes:
        return False
    if (fixes_sampling(attributes[ATTR_API_VERSION])
            and any(_sent(kwargs, k) for k in _SAMPLING_KEYS)):
        logger.warning(
            "an Anthropic call on API version %s sends %s; that version rejects every "
            "sampling parameter (HTTP 400). Remove it: sampling is fixed at temperature 1.0, "
            "which is what pricing needs.",
            attributes[ATTR_API_VERSION],
            ", ".join(k for k in _SAMPLING_KEYS if _sent(kwargs, k)))
    span.set_attributes(attributes)
    return True
