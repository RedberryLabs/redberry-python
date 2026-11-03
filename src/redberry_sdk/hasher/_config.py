"""The hasher's options, with defaults from the same environment variables as ``@redberrylabs/otel``.

Nothing here raises: a misconfigured hasher logs one warning and does nothing, rather than breaking
the customer's function at start-up.

=================================  =======================================================================
``REDBERRY_URL``                   VALP's base URL
``REDBERRY_PROJECT_TOKEN``         the project token (64 lowercase hex)
``REDBERRY_SIGNING_KEY``           base64 PKCS8 Ed25519 private key (``redberry connect`` writes it)
``REDBERRY_KEY_ID``                optional; checked against the key
``REDBERRY_AGENT_ID``              the agent every trace belongs to, unless something more specific matches
``REDBERRY_AGENTS``                JSON ``{route: agent id}``, matched against the root span's routes
``REDBERRY_STORE``                 ``redberry`` (default), ``azure-blob`` or ``s3``
``REDBERRY_AZURE_CONTAINER_URL``   ``https://<account>.blob.core.windows.net/<container>`` (``azure-blob``)
``REDBERRY_AZURE_CLIENT_ID``       the user-assigned managed identity (``azure-blob``; unset: system-assigned);
                                   ``AZURE_CLIENT_ID`` when unset
``REDBERRY_AZURE_TENANT_ID``       with a federated token file (``azure-blob``, client-assertion mode);
                                   ``AZURE_TENANT_ID`` when unset
``REDBERRY_S3_BUCKET``             ``s3://<bucket>`` (``s3``)
``REDBERRY_S3_REGION``             the bucket's region (``s3``); ``AWS_REGION`` when unset
``REDBERRY_AWS_ROLE_ARN``          (``s3``, off AWS) a role assumed with a web identity token
                                   (``AWS_WEB_IDENTITY_TOKEN_FILE`` or ``VERCEL_OIDC_TOKEN``); on Lambda,
                                   unset: the execution role's credentials
``REDBERRY_DEADLINE_MS``           the function's time budget; a trace still open ``REDBERRY_DEADLINE_MARGIN_MS``
                                   (default 5000) before it is cut short and stored
``REDBERRY_DISABLED=1``            turns the hasher off
``REDBERRY_ALL_ENVS=1``            runs it anywhere (local development, tests)
=================================  =======================================================================

**Where it runs.** On Azure Functions and App Service (``WEBSITE_SITE_NAME`` is set) and on AWS Lambda
(``AWS_LAMBDA_FUNCTION_NAME`` is set), unless ``REDBERRY_DISABLED=1``; on Vercel, in production
(``VERCEL_ENV=production``) only, as the package does; elsewhere only with ``REDBERRY_ALL_ENVS=1``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ._agents import is_agent_id
from ._http import DEFAULT_RETRY_DELAYS, Transport
from .stores import Store, store_from_env

__all__ = ["HasherConfig", "resolve_config"]

log = logging.getLogger("redberry.hasher")
_TOKEN = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class HasherConfig:
    enabled: bool = False
    disabled_reason: str | None = None
    agent_id: str | None = None
    agents_by_route: Mapping[str, str] | None = None
    store: Store | None = None
    url: str = ""
    token: str = ""
    signing_key: str = ""
    key_id: str | None = None
    #: Quiet period after the root ends and nothing is open, before finalising (ms).
    grace_ms: float = 2000
    #: Finalise as cut_short this long after the root ended even if spans are still open (ms).
    hard_stop_ms: float = 60_000
    #: The function's time budget (ms); unset: no deadline timer.
    deadline_ms: float | None = None
    deadline_margin_ms: float = 5000
    #: Only hash traces with a model or agent span.
    require_ai_spans: bool = True
    max_traces: int = 1000
    max_spans_per_trace: int = 10_000
    on_result: Callable[[Any], None] | None = None
    transport: Transport | None = None
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS


def _num(v: str | None) -> float | None:
    if v is None or not v.strip():
        return None
    try:
        n = float(v)
    except ValueError:
        return None
    return n if n >= 0 and n != float("inf") else None


def resolve_config(*, env: Mapping[str, str] | None = None, agent_id: str | None = None,
                   agents_by_route: Mapping[str, str] | None = None, store: Store | None = None,
                   url: str | None = None, token: str | None = None, signing_key: str | None = None,
                   key_id: str | None = None, all_envs: bool | None = None, grace_ms: float | None = None,
                   hard_stop_ms: float | None = None, deadline_ms: float | None = None,
                   deadline_margin_ms: float | None = None, require_ai_spans: bool = True, max_traces: int = 1000,
                   max_spans_per_trace: int = 10_000, on_result: Callable[[Any], None] | None = None,
                   transport: Transport | None = None,
                   retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS) -> HasherConfig:
    env = os.environ if env is None else env
    base = HasherConfig(
        url=url if url is not None else env.get("REDBERRY_URL", ""),
        token=token if token is not None else env.get("REDBERRY_PROJECT_TOKEN", ""),
        signing_key=signing_key if signing_key is not None else env.get("REDBERRY_SIGNING_KEY", ""),
        key_id=key_id if key_id is not None else (env.get("REDBERRY_KEY_ID") or None),
        agent_id=agent_id if agent_id is not None else (env.get("REDBERRY_AGENT_ID") or None),
        grace_ms=2000 if grace_ms is None else grace_ms,
        hard_stop_ms=60_000 if hard_stop_ms is None else hard_stop_ms,
        deadline_ms=deadline_ms if deadline_ms is not None else _num(env.get("REDBERRY_DEADLINE_MS")),
        deadline_margin_ms=(deadline_margin_ms if deadline_margin_ms is not None
                            else _num(env.get("REDBERRY_DEADLINE_MARGIN_MS")) or 5000),
        require_ai_spans=require_ai_spans, max_traces=max_traces, max_spans_per_trace=max_spans_per_trace,
        on_result=on_result, transport=transport, retry_delays=retry_delays,
    )

    def off(reason: str, warn: bool = True) -> HasherConfig:
        if warn:
            log.warning("[redberry] disabled: %s", reason)
        return replace(base, enabled=False, disabled_reason=reason)

    if env.get("REDBERRY_DISABLED") == "1":
        return off("REDBERRY_DISABLED=1", warn=False)
    everywhere = all_envs if all_envs is not None else env.get("REDBERRY_ALL_ENVS") == "1"
    if not (everywhere or env.get("WEBSITE_SITE_NAME") or env.get("AWS_LAMBDA_FUNCTION_NAME")
            or env.get("VERCEL_ENV") == "production"):
        return off("not on Azure Functions or App Service (WEBSITE_SITE_NAME is unset), AWS Lambda "
                   "(AWS_LAMBDA_FUNCTION_NAME is unset) or in Vercel production; set REDBERRY_ALL_ENVS=1 to run here",
                   warn=False)

    routes = agents_by_route
    if routes is None and env.get("REDBERRY_AGENTS"):
        try:
            parsed = json.loads(env["REDBERRY_AGENTS"])
            if not isinstance(parsed, dict):
                raise TypeError("not an object")
            routes = {str(k): v for k, v in parsed.items()}
        except (TypeError, ValueError) as e:
            return off(f"REDBERRY_AGENTS is not a JSON object of route -> agent id ({e})")
    agents = [*(routes or {}).values(), *([base.agent_id] if base.agent_id else [])]
    bad = [str(a) for a in agents if not is_agent_id(a)]
    if bad:
        return off(f"not agent ids (AGT- and a ULID): {', '.join(bad)}")
    if not agents:
        log.info("[redberry] no default agent (REDBERRY_AGENT_ID / REDBERRY_AGENTS): only handlers wrapped with "
                 "with_redberry(agent_id=...) are hashed")
    if not base.url:
        return off("REDBERRY_URL is not set")
    parts = urllib.parse.urlsplit(base.url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return off(f"REDBERRY_URL is not a URL: {base.url}")
    if not _TOKEN.fullmatch(base.token or ""):
        return off("REDBERRY_PROJECT_TOKEN is not set or not 64 lowercase hex characters")
    if not base.signing_key:
        return off("REDBERRY_SIGNING_KEY is not set")
    if base.key_id is not None and not _KEY_ID.fullmatch(base.key_id):
        return off("REDBERRY_KEY_ID is not 32 lowercase hex characters")
    if store is None:
        try:
            store = store_from_env(env, url=base.url, token=base.token, transport=transport,
                                   retry_delays=retry_delays)
        except ValueError as e:
            return off(str(e))
    return replace(base, agents_by_route=routes, store=store, enabled=True)
