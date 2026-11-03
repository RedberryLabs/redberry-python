"""The agent's emit path: where spans go, how they authenticate, and what is dropped.

RAP docs/client_install_architecture.md, principles 1, 6 and 7 and section 2.2. The agent only emits:
nothing here hashes, signs, holds a key or writes to storage. Nothing here raises into the agent: a
discovery or token failure is logged once and left to the OTLP exporter's own retry and backoff.

Endpoint, in order:

1. ``REDBERRY_INGEST_URL``, ``REDBERRY_OTEL_ENDPOINT`` or an explicit ``otel_endpoint`` (kwarg / redberry.yaml).
2. On GCP, when the agent is named (``init(agent=...)``): the metadata server gives the project number
   and region, and the ingest service is ``https://redberry-ingest-<project-number>.<region>.run.app``.
   Cloud Run reports the region as ``projects/<n>/regions/<region>``; GCE and GKE may report none, and
   then ``REDBERRY_GCP_REGION`` must say it (logged once, then step 3).
   On AWS (Lambda, ECS, or ``REDBERRY_CLOUD=aws``), when the agent is named: the account (from
   ``sts:GetCallerIdentity``) and ``AWS_REGION`` name the installation's bucket, whose ``ingest.json``
   gives the ingest function URL (``redberry_sdk._emit_aws``); each export is SigV4-signed.
   On Azure (Functions, Container Apps, App Service, or ``REDBERRY_CLOUD=azure``), when the agent is
   named: the managed identity's token for Redberry's app asks ``/api/discover/azure`` for the ingest
   URL (``redberry_sdk._emit_azure``); each export carries that token as a Bearer.
3. ``http://localhost:4318``, the Docker bundle's collector.

On GCP every export carries ``Authorization: Bearer <ID token>``, the token minted by the metadata
server for the ingest base URL and cached until five minutes before it expires.

A new install's ``roles/run.invoker`` grant takes seconds to minutes to propagate, and a token can be
briefly stale, so a 401 or 403 from ingest is retried (the batch, with backoff, on the export thread)
for ``REDBERRY_AUTH_RETRY_S`` seconds (default 300) before the spans count as dropped; a 401 also
drops the cached token. The OTLP exporter itself gives up on both at once.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass

import requests
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from . import _env

_log = logging.getLogger("redberry.sdk")

DEFAULT_ENDPOINT = "http://localhost:4318"
INGEST_SERVICE = "redberry-ingest"
ENDPOINT_ENV = "REDBERRY_OTEL_ENDPOINT"
INGEST_URL_ENV = "REDBERRY_INGEST_URL"
REGION_ENV = "REDBERRY_GCP_REGION"
#: google-auth's override for the metadata server's host (also how the tests point at a fake one).
METADATA_HOST_ENV = "GCE_METADATA_HOST"
METADATA_TIMEOUT_S = 1.0
TOKEN_REFRESH_MARGIN_S = 300.0
#: The bounded buffer: at most this many spans wait for export; beyond it they are dropped and counted.
MAX_QUEUE_SIZE = 2048
DROPPED_LOG_INTERVAL_S = 60.0
AUTH_RETRY_ENV = "REDBERRY_AUTH_RETRY_S"
AUTH_RETRY_DEFAULT_S = 300.0
AUTH_RETRY_FIRST_S = 2.0
AUTH_RETRY_MAX_S = 32.0
AUTH_RETRY_STATUSES = (401, 403)

AGENT_NAME_ATTRIBUTE = "redberry.agent.name"
AGENT_NAME_MAX = 64


# ── agent name ───────────────────────────────────────────────────────────────────────────────────

def validate_agent_name(name: object) -> str:
    """The name the customer gives the agent: a string of 1 to 64 printable characters (no control
    characters, no line breaks; spaces allowed but not at either end). Ingest maps it to the
    agent id Redberry generates. Raises ValueError (TypeError for a non-string), at init(), before anything is set up."""
    if not isinstance(name, str):
        raise TypeError(f"agent must be a string, not {type(name).__name__}")
    if not 1 <= len(name) <= AGENT_NAME_MAX:
        raise ValueError(f"agent must be 1 to {AGENT_NAME_MAX} characters, not {len(name)}")
    if not name.isprintable() or name != name.strip():
        raise ValueError(f"agent must be printable characters without leading or trailing spaces: {name!r}")
    return name


# ── the metadata server ──────────────────────────────────────────────────────────────────────────

def _metadata_base() -> str:
    host = os.environ.get(METADATA_HOST_ENV, "").strip() or "metadata.google.internal"
    return f"http://{host}/computeMetadata/v1/"


def _metadata_get(path: str, timeout: float = METADATA_TIMEOUT_S) -> str | None:
    """One metadata value, or None when there is no metadata server or no such value."""
    request = urllib.request.Request(_metadata_base() + path, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.headers.get("Metadata-Flavor") != "Google":
                return None
            return response.read().decode().strip()
    except (OSError, ValueError, urllib.error.URLError):
        return None


def ingest_url(project_number: str, region: str) -> str:
    """The ingest service's deterministic Cloud Run URL."""
    return f"https://{INGEST_SERVICE}-{project_number}.{region}.run.app"


@dataclass(frozen=True)
class Endpoint:
    url: str  # base URL; the exporter posts to <url>/v1/traces
    source: str  # "explicit", "gcp", "aws", "azure" or "default"
    audience: str | None = None  # the ID token's audience, on GCP only
    aws_region: str | None = None  # the SigV4 region, on AWS only
    aws_credentials: object = None  # _emit_aws.AwsCredentials, on AWS only
    azure_token: object = None  # _emit_azure.ManagedIdentityToken, on Azure only


_ENDPOINT_CACHE: Endpoint | None = None
_LOGGED_ONCE: set[str] = set()
_ONCE_LOCK = threading.Lock()


def _log_once(key: str, level: int, message: str, *args: object) -> None:
    with _ONCE_LOCK:
        if key in _LOGGED_ONCE:
            return
        _LOGGED_ONCE.add(key)
    _log.log(level, message, *args)


def discover_gcp() -> Endpoint | None:
    """The GCP ingest endpoint, or None when not on GCP (or the region cannot be known)."""
    project_number = _metadata_get("project/numeric-project-id")
    if not project_number:
        return None
    region = (_env.get(REGION_ENV) or "").strip()
    if not region:
        reported = _metadata_get("instance/region") or ""
        region = reported.rsplit("/", 1)[-1] if reported else ""
    if not region:
        _log_once("region", logging.WARNING,
                  "redberry_sdk: on GCP but the metadata server reports no region (GCE/GKE); set %s to "
                  "the region of the redberry-ingest service. Sending to %s meanwhile.", REGION_ENV,
                  DEFAULT_ENDPOINT)
        return None
    url = ingest_url(project_number, region)
    return Endpoint(url=url, source="gcp", audience=url)


def discover_aws() -> Endpoint | None:
    """The AWS ingest endpoint, or None when not on AWS or discovery fails (logged once)."""
    from . import _emit_aws
    if not _emit_aws.on_aws():
        return None
    try:
        url, region, creds = _emit_aws.discover()
    except Exception as e:  # noqa: BLE001 - never fail init over discovery
        _log_once("aws-discovery", logging.WARNING,
                  "redberry_sdk: on AWS but the Redberry ingest endpoint could not be discovered (%s); "
                  "set %s, or check the installation. Sending to %s meanwhile.", e, INGEST_URL_ENV,
                  DEFAULT_ENDPOINT)
        return None
    return Endpoint(url=url, source="aws", aws_region=region, aws_credentials=creds)


def discover_azure() -> Endpoint | None:
    """The Azure ingest endpoint, or None when not on Azure or discovery fails (logged once)."""
    from . import _emit_azure
    if not _emit_azure.on_azure():
        return None
    try:
        url, tokens = _emit_azure.discover()
    except Exception as e:  # noqa: BLE001 - never fail init over discovery
        _log_once("azure-discovery", logging.WARNING,
                  "redberry_sdk: on Azure but the Redberry ingest endpoint could not be discovered (%s); "
                  "set %s, or check the installation and the agent's managed identity. Sending to %s "
                  "meanwhile.", e, INGEST_URL_ENV, DEFAULT_ENDPOINT)
        return None
    return Endpoint(url=url, source="azure", azure_token=tokens)


def cloud_run_resource_attributes() -> dict[str, str]:
    """OpenTelemetry semantic-convention attributes for where the agent runs: on Cloud Run, and on AWS
    Lambda (``faas.name`` from ``AWS_LAMBDA_FUNCTION_NAME``, ``cloud.region``), and on Azure Functions,
    Container Apps and App Service (``redberry_sdk._emit_azure.resource_attributes``).

    Empty elsewhere (neither ``K_SERVICE`` nor ``AWS_LAMBDA_FUNCTION_NAME`` set). Each value is best effort: a missing env var or
    metadata value drops that attribute, never fails init.
    """
    from . import _emit_azure
    azure = _emit_azure.resource_attributes()
    if azure:
        return azure
    function = (os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "").strip()
    if function:
        # AWS Lambda: the scoring job reads the function's code by name and region (lambda:GetFunction).
        attrs = {"cloud.provider": "aws", "cloud.platform": "aws_lambda", "faas.name": function}
        for env, key in (("AWS_LAMBDA_FUNCTION_VERSION", "faas.version"), ("AWS_REGION", "cloud.region")):
            value = (os.environ.get(env) or "").strip()
            if value:
                attrs[key] = value
        return attrs
    service = (os.environ.get("K_SERVICE") or "").strip()
    if not service:
        return {}
    attrs = {"cloud.provider": "gcp", "cloud.platform": "gcp_cloud_run", "faas.name": service}
    revision = (os.environ.get("K_REVISION") or "").strip()
    if revision:
        attrs["faas.version"] = revision
    try:
        region = (_metadata_get("instance/region") or "").rsplit("/", 1)[-1].strip()
        if region:
            attrs["cloud.region"] = region
        project = (_metadata_get("project/project-id") or "").strip()
        if project:
            attrs["cloud.account.id"] = project
    except Exception:  # noqa: BLE001 - never fail init over location metadata
        _log.debug("redberry_sdk: Cloud Run location metadata unavailable", exc_info=True)
    return attrs


def resolve_endpoint(configured: str, *, discover: bool) -> Endpoint:
    """Where spans go (see the module docstring). ``configured`` is SDKConfig.otel_endpoint after the
    env overrides; anything but the default, or the env variable set at all, is explicit. Discovery
    runs only when ``discover`` (the agent is named) and its result is cached for the process."""
    global _ENDPOINT_CACHE
    override = (os.environ.get(INGEST_URL_ENV) or "").strip()
    if override:
        return Endpoint(url=override.rstrip("/"), source="explicit")
    if (_env.get(ENDPOINT_ENV) or "").strip() or configured.rstrip("/") != DEFAULT_ENDPOINT:
        return Endpoint(url=configured.rstrip("/"), source="explicit")
    if not discover:
        return Endpoint(url=DEFAULT_ENDPOINT, source="default")
    if _ENDPOINT_CACHE is None:
        _ENDPOINT_CACHE = discover_aws() or discover_azure() or discover_gcp() or Endpoint(url=DEFAULT_ENDPOINT, source="default")
    return _ENDPOINT_CACHE


# ── the ID token ─────────────────────────────────────────────────────────────────────────────────

class TokenUnavailable(requests.exceptions.ConnectionError):
    """No ID token. A ConnectionError, so the OTLP exporter treats the attempt as transient and
    retries with backoff; it never reaches the agent."""


def _jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None


class IdTokenAuth(requests.auth.AuthBase):
    """Adds ``Authorization: Bearer <ID token>`` to each export request. The token comes from the
    metadata server's identity endpoint for ``audience`` and is reused until five minutes before
    its ``exp`` (a token whose expiry cannot be read is reused for 30 minutes)."""

    def __init__(self, audience: str, clock=time.time) -> None:
        self.audience = audience
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._refresh_at = 0.0
        self._failed_logged = False

    def token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._refresh_at:
                return self._token
            query = urllib.parse.urlencode({"audience": self.audience, "format": "full"})
            token = _metadata_get(f"instance/service-accounts/default/identity?{query}")
            if not token:
                if not self._failed_logged:
                    self._failed_logged = True
                    _log.warning("redberry_sdk: no ID token from the metadata server for %s; spans stay "
                                 "queued and the export is retried", self.audience)
                raise TokenUnavailable(f"no ID token for {self.audience}")
            expiry = _jwt_expiry(token)
            self._token = token
            self._refresh_at = (expiry - TOKEN_REFRESH_MARGIN_S) if expiry else now + 1800.0
            self._failed_logged = False
            return token

    def invalidate(self) -> None:
        """Forget the cached token (ingest answered 401): the next export fetches a fresh one."""
        with self._lock:
            self._token = None
            self._refresh_at = 0.0

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self.token()}"
        return request


def build_session(endpoint: Endpoint) -> requests.Session | None:
    """The exporter's HTTP session: with the ID-token hook on GCP, SigV4 on AWS, the managed identity's token on Azure, else None (the exporter's own)."""
    if endpoint.source == "aws" and endpoint.aws_credentials is not None:
        from ._emit_aws import SigV4Auth
        session = requests.Session()
        session.auth = SigV4Auth(endpoint.aws_credentials, endpoint.aws_region or "")
        return session
    if endpoint.source == "azure" and endpoint.azure_token is not None:
        from ._emit_azure import BearerAuth
        session = requests.Session()
        session.auth = BearerAuth(endpoint.azure_token)
        return session
    if endpoint.audience is None:
        return None
    session = requests.Session()
    session.auth = IdTokenAuth(endpoint.audience)
    return session


# ── dropped spans ────────────────────────────────────────────────────────────────────────────────

_DROPPED = 0
_DROPPED_LOGGED = 0
_DROPPED_LOGGED_AT = 0.0
_DROPPED_LOCK = threading.Lock()


def dropped_spans() -> int:
    """Spans this process has dropped: the export queue was full, or an export failed after the
    exporter's retries (for a 401/403, after ``REDBERRY_AUTH_RETRY_S``). Zero means nothing was lost on the agent's side."""
    return _DROPPED


def _count_dropped(n: int) -> None:
    global _DROPPED
    with _DROPPED_LOCK:
        _DROPPED += n
    _maybe_log_dropped()


def _maybe_log_dropped(now: float | None = None) -> None:
    """Log the count when it has grown, at most once per DROPPED_LOG_INTERVAL_S."""
    global _DROPPED_LOGGED, _DROPPED_LOGGED_AT
    now = time.monotonic() if now is None else now
    with _DROPPED_LOCK:
        if _DROPPED == _DROPPED_LOGGED or (_DROPPED_LOGGED_AT and now - _DROPPED_LOGGED_AT < DROPPED_LOG_INTERVAL_S):
            return
        _DROPPED_LOGGED, _DROPPED_LOGGED_AT = _DROPPED, now
        total = _DROPPED
    _log.warning("redberry_sdk: %d span(s) dropped so far (redberry_sdk.dropped_spans())", total)


class CountingExporter(SpanExporter):
    """Counts the spans of a batch the inner exporter gave up on, and never lets it raise."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self.inner.export(spans)
        except Exception as e:  # noqa: BLE001 (the agent must never see it)
            _log_once("export-raised", logging.WARNING, "redberry_sdk: span export failed: %s", e)
            result = SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            _count_dropped(len(spans))
        else:
            _maybe_log_dropped()
        return result

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def auth_retry_budget() -> float:
    """Seconds a batch refused with 401/403 is retried: ``REDBERRY_AUTH_RETRY_S``, default 300."""
    raw = (_env.get(AUTH_RETRY_ENV) or "").strip()
    try:
        value = float(raw) if raw else AUTH_RETRY_DEFAULT_S
    except ValueError:
        _log_once("auth-retry-env", logging.WARNING, "redberry_sdk: %s=%r is not a number of seconds; "
                  "using %d", AUTH_RETRY_ENV, raw, AUTH_RETRY_DEFAULT_S)
        return AUTH_RETRY_DEFAULT_S
    return max(0.0, value)


def _service_account() -> str:
    return _metadata_get("instance/service-accounts/default/email") or "the agent's service account"


class AuthRetryExporter(SpanExporter):
    """Retries a batch ingest refused with 401 or 403, on the exporter's own (background) thread.

    The OTLP exporter reports only FAILURE, so the status is read from a response hook on its
    requests session. A 401 drops the cached ID token. Waits are 2, 4, 8, 16, 32, 32... seconds,
    within ``budget_s`` from the first refusal; ``stop()`` (shutdown) ends them at once and
    ``flush_deadline()`` (force_flush) bounds them. Then the batch fails (and CountingExporter
    counts it), with one log line naming the grant that is missing."""

    def __init__(self, inner: SpanExporter, session: requests.Session, auth=None,
                 budget_s: float | None = None) -> None:
        self.inner = inner
        self._auth = auth
        self._budget_s = auth_retry_budget() if budget_s is None else budget_s
        self._status = threading.local()
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._flush_until: float | None = None
        session.hooks.setdefault("response", []).append(self._record_status)

    def _record_status(self, response, *args, **kwargs):
        self._status.code = getattr(response, "status_code", None)
        return response

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        give_up_at: float | None = None
        delay = AUTH_RETRY_FIRST_S
        while True:
            self._status.code = None
            result = self.inner.export(spans)
            code = self._status.code
            if result is SpanExportResult.SUCCESS or code not in AUTH_RETRY_STATUSES:
                return result
            if code == 401 and self._auth is not None:
                self._auth.invalidate()
            now = time.monotonic()
            if give_up_at is None:
                give_up_at = now + self._budget_s
            if not self._wait(min(delay, give_up_at - now)):
                self._refused(code)
                return SpanExportResult.FAILURE
            delay = min(delay * 2, AUTH_RETRY_MAX_S)

    def _wait(self, seconds: float) -> bool:
        """Sleep ``seconds`` unless stopped or past a flush deadline; True to try again."""
        end = time.monotonic() + seconds
        while True:
            if self._stopped.is_set():
                return False
            now = time.monotonic()
            flush_until = self._flush_until
            if seconds <= 0 or (flush_until is not None and now >= flush_until):
                return False
            if now >= end:
                return True
            self._wake.clear()
            limit = end if flush_until is None else min(end, flush_until)
            self._wake.wait(max(0.0, limit - now))

    def _refused(self, code: int) -> None:
        if type(self._auth).__name__ == "SigV4Auth":
            _log_once(f"auth-refused-{code}", logging.ERROR,
                      "redberry_sdk: ingest refused the agent's IAM role (%d): the installation grants "
                      "lambda:InvokeFunctionUrl to this account on Redberry's ingest function; check the "
                      "install. Spans are dropped until it is granted.", code)
            return
        who = _service_account() if self._auth is not None else "the agent's identity"
        _log_once(f"auth-refused-{code}", logging.ERROR,
                  "redberry_sdk: ingest refused the agent's identity (%d): grant roles/run.invoker on "
                  "%s to %s — `redberry` install script step 13. Spans are dropped until it is granted.",
                  code, INGEST_SERVICE, who)

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    def flush_deadline(self, until: float | None) -> None:
        self._flush_until = until
        self._wake.set()

    def shutdown(self) -> None:
        self.stop()
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


class CountingBatchSpanProcessor(BatchSpanProcessor):
    """BatchSpanProcessor with a bounded queue that counts what it drops when full. It never
    blocks: a full queue drops (the oldest span, as BatchSpanProcessor does) and counts one."""

    def __init__(self, exporter: SpanExporter, max_queue_size: int = MAX_QUEUE_SIZE,
                 auth_retry: AuthRetryExporter | None = None, **kwargs) -> None:
        super().__init__(exporter, max_queue_size=max_queue_size, **kwargs)
        self._max_queue = max_queue_size
        self._auth_retry = auth_retry

    def shutdown(self) -> None:
        if self._auth_retry is not None:
            self._auth_retry.stop()  # an export waiting out a 401/403 ends now, not at the join timeout
        super().shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        if self._auth_retry is None:
            return super().force_flush(timeout_millis)
        self._auth_retry.flush_deadline(time.monotonic() + timeout_millis / 1000)
        try:
            return super().force_flush(timeout_millis)
        finally:
            self._auth_retry.flush_deadline(None)

    def on_end(self, span: ReadableSpan) -> None:
        try:
            queue = getattr(getattr(self, "_batch_processor", None), "_queue", None)
            if queue is not None and len(queue) >= self._max_queue and span.context and span.context.trace_flags.sampled:
                _count_dropped(1)
            super().on_end(span)
        except Exception as e:  # noqa: BLE001
            _log_once("on-end-raised", logging.WARNING, "redberry_sdk: span not queued: %s", e)
            _count_dropped(1)


def build_processor(endpoint: Endpoint, wrap=lambda exporter: exporter) -> CountingBatchSpanProcessor:
    """The OTLP/HTTP exporter for ``endpoint`` (with the ID-token session on GCP), wrapped by
    ``wrap`` (the valp.* sequencing), counted, behind a bounded batch processor."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    session = build_session(endpoint) or requests.Session()
    # 30 s, not OTLP's 10: the first export may wait on a cold-starting ingest (a Lambda container, ~5 s).
    otlp = OTLPSpanExporter(endpoint=f"{endpoint.url}/v1/traces", session=session, timeout=30)
    auth = session.auth if hasattr(session.auth, "invalidate") else None
    retry = AuthRetryExporter(otlp, session, auth)
    return CountingBatchSpanProcessor(CountingExporter(wrap(retry)), auth_retry=retry)


def _reset_for_tests() -> None:
    global _ENDPOINT_CACHE, _DROPPED, _DROPPED_LOGGED, _DROPPED_LOGGED_AT
    _ENDPOINT_CACHE = None
    _DROPPED = _DROPPED_LOGGED = 0
    _DROPPED_LOGGED_AT = 0.0
    _LOGGED_ONCE.clear()
