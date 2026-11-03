"""Where a run's stored bytes go (``@redberrylabs/otel`` SPEC.md §5), on plain HTTP (no cloud SDK).

``Store.put`` creates the object or fails: it never overwrites. It raises ``StoreConflictError`` when
an object is already at the path, and any other exception when the write failed. If the write fails,
the hash report still goes out, with ``stored: false``.

=================  ========================================================================================
``redberry``       Redberry's bucket. VALP hands out a signed, create-only PUT pinned to the run's size and
                   SHA-256, and the bytes go straight to the bucket (VALP never handles them)::

                       POST {REDBERRY_URL}/v1/agents/{agent_id}/stored-logs/upload-url
                            Authorization: Bearer <project token>   {run_id, sha256, size}
                         -> {url, method: "PUT", headers, object, expires_at}
                       PUT  url  with exactly those headers

                   A 412 on the PUT means the object exists; VALP signed that name for this exact size and
                   hash, so the bytes there are ours (a retry), reported as stored.
``azure-blob``     The customer's Azure Blob container (``REDBERRY_AZURE_CONTAINER_URL``), keyless: a
                   Microsoft Entra ID token for ``https://storage.azure.com/`` from the app's managed
                   identity (``IDENTITY_ENDPOINT`` / ``IDENTITY_HEADER``, as on Azure Functions and App
                   Service; ``REDBERRY_AZURE_CLIENT_ID`` names a user-assigned identity), or from a client
                   assertion (a federated token, ``AZURE_FEDERATED_TOKEN_FILE``)::

                       PUT {container url}/{path}
                           Authorization: Bearer <token>   x-ms-version: 2023-11-03
                           x-ms-blob-type: BlockBlob        If-None-Match: *
                           content-type: application/gzip   x-ms-meta-sha256: <hex>

                   409 (``BlobAlreadyExists``) is an object already there. The container's time-based
                   immutability policy keeps stored runs from being changed or deleted until it expires.
``s3``             The customer's S3 bucket (``REDBERRY_S3_BUCKET``, ``<bucket>`` or ``s3://<bucket>``, in
                   ``REDBERRY_S3_REGION``, else ``AWS_REGION``), SigV4-signed (``_sigv4``)::

                       PUT https://<bucket>.s3.<region>.amazonaws.com/<path>
                           Authorization: AWS4-HMAC-SHA256 ...   x-amz-content-sha256: <hex>
                           If-None-Match: *                       content-type: application/gzip
                           x-amz-meta-sha256: <hex>               x-amz-checksum-sha256, Content-MD5

                   The credentials are the standard environment's (``AWS_ACCESS_KEY_ID``,
                   ``AWS_SECRET_ACCESS_KEY``, ``AWS_SESSION_TOKEN``: on AWS Lambda, the execution role's), or
                   a role assumed with a web identity token (``REDBERRY_AWS_ROLE_ARN`` or ``AWS_ROLE_ARN``,
                   with ``AWS_WEB_IDENTITY_TOKEN_FILE`` or Vercel's ``VERCEL_OIDC_TOKEN``) through STS
                   ``AssumeRoleWithWebIdentity``. 412 (``PreconditionFailed``) or 409
                   (``ConditionalRequestConflict``) is an object already there. The bucket's Object Lock
                   default retention keeps stored runs from being changed or deleted until it expires (an
                   Object Lock bucket wants a checksum on every PUT: both are sent).
=================  ========================================================================================
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.utils import formatdate
from typing import Protocol, runtime_checkable

from ._http import (
    DEFAULT_RETRY_DELAYS,
    Request,
    Response,
    Transport,
    _suppressed,
    describe,
    send,
    trim_base,
    urllib_transport,
    with_retries,
)
from ._sigv4 import Credentials, sign

__all__ = [
    "STORE_KINDS",
    "AwsCredentialProvider",
    "AzureBlobStore",
    "ClientAssertionCredential",
    "EnvAwsCredentials",
    "ManagedIdentityCredential",
    "PutMeta",
    "PutResult",
    "RedberryStore",
    "S3Store",
    "Store",
    "StoreConflictError",
    "TokenCredential",
    "WebIdentityCredentials",
    "aws_credentials_from_env",
    "credential_from_env",
    "store_from_env",
]

#: The ``store`` values this hasher writes (VALP ``hashes.STORES`` accepts these and more).
STORE_KINDS = ("redberry", "azure-blob", "s3")
AZURE_STORAGE_RESOURCE = "https://storage.azure.com/"
AZURE_STORAGE_SCOPE = "https://storage.azure.com/.default"
AZURE_BLOB_API_VERSION = "2023-11-03"
AZURE_AUTHORITY = "https://login.microsoftonline.com"
#: A token is used until this long before it expires (seconds).
TOKEN_REFRESH_MARGIN = 300.0


@dataclass(frozen=True)
class PutMeta:
    """What a store may need besides the path and bytes (the Redberry store names the run to VALP)."""
    agent_id: str
    trace_id: str
    #: Lowercase hex SHA-256 of the bytes.
    sha256: str
    size: int


@dataclass(frozen=True)
class PutResult:
    #: Where the object is, if the store says (private: not readable without credentials).
    url: str | None = None
    #: True when the object was already there and the store guarantees it holds these exact bytes.
    existing: bool = False


class StoreConflictError(Exception):
    def __init__(self, path: str, detail: str | None = None) -> None:
        super().__init__(f"an object already exists at {path}" + (f" ({detail})" if detail else ""))


@runtime_checkable
class Store(Protocol):
    @property
    def kind(self) -> str: ...

    def put(self, path: str, data: bytes, meta: PutMeta) -> PutResult: ...


# ── redberry ─────────────────────────────────────────────────────────────────────────────────────


class RedberryStore:
    kind = "redberry"

    def __init__(self, url: str | None = None, token: str | None = None, *, transport: Transport | None = None,
                 retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.url, self.token = url, token
        self.transport = transport or urllib_transport
        self.retry_delays, self.sleep = retry_delays, sleep

    def put(self, path: str, data: bytes, meta: PutMeta) -> PutResult:
        base = self.url or os.environ.get("REDBERRY_URL")
        token = self.token or os.environ.get("REDBERRY_PROJECT_TOKEN")
        if not base or not token:
            raise RuntimeError("redberry store: REDBERRY_URL and REDBERRY_PROJECT_TOKEN are required")
        body = json.dumps({"run_id": meta.trace_id, "sha256": meta.sha256, "size": meta.size},
                          separators=(",", ":")).encode()
        ask = send(self.transport, Request(
            "POST", f"{trim_base(base)}/v1/agents/{urllib.parse.quote(meta.agent_id, safe='')}/stored-logs/upload-url",
            {"authorization": f"Bearer {token}", "content-type": "application/json"}, body),
            self.retry_delays, self.sleep)
        if ask.response is None:
            raise RuntimeError(f"redberry store: upload-url failed: {ask.error}")
        if not ask.response.ok:
            raise RuntimeError(f"redberry store: upload-url refused: {describe(ask.response)}")
        signed = ask.response.json()
        if not isinstance(signed, dict) or not isinstance(signed.get("url"), str) or not signed["url"]:
            raise RuntimeError("redberry store: upload-url reply has no url")
        headers = signed.get("headers")
        put = send(self.transport, Request(
            str(signed.get("method") or "PUT"), signed["url"],
            {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict)
            else {"content-type": "application/gzip"}, data),
            self.retry_delays, self.sleep)
        if put.response is None:
            raise RuntimeError(f"redberry store: upload failed: {put.error}")
        if put.response.status == 412:
            return PutResult(url=signed.get("object"), existing=True)
        if not put.response.ok:
            raise RuntimeError(f"redberry store: upload failed: {describe(put.response)}")
        return PutResult(url=signed.get("object"))


# ── azure-blob ───────────────────────────────────────────────────────────────────────────────────


class TokenCredential(Protocol):
    def token(self) -> str: ...

    def invalidate(self) -> None: ...


class _CachedToken:
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._expires_at - TOKEN_REFRESH_MARGIN <= self._now():
                self._token, self._expires_at = self._mint()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _mint(self) -> tuple[str, float]:
        raise NotImplementedError


class ManagedIdentityCredential(_CachedToken):
    """The App Service / Azure Functions managed identity endpoint::

        GET ${IDENTITY_ENDPOINT}?resource=https://storage.azure.com/&api-version=2019-08-01&client_id=<id>
            X-IDENTITY-HEADER: ${IDENTITY_HEADER}

    ``client_id`` names a user-assigned identity; without it, the system-assigned one."""

    def __init__(self, client_id: str | None = None, *, endpoint: str | None = None, header: str | None = None,
                 transport: Transport | None = None, retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time) -> None:
        super().__init__(now)
        self.client_id = client_id or None
        self.endpoint = endpoint or os.environ.get("IDENTITY_ENDPOINT") or ""
        self._header = header or os.environ.get("IDENTITY_HEADER") or ""
        self.transport = transport or urllib_transport
        self.retry_delays, self.sleep = retry_delays, sleep
        if not self.endpoint or not self._header:
            raise ValueError("managed identity: IDENTITY_ENDPOINT and IDENTITY_HEADER are not set (is an identity "
                             "assigned to this app?)")

    def _mint(self) -> tuple[str, float]:
        query = {"resource": AZURE_STORAGE_RESOURCE, "api-version": "2019-08-01"}
        if self.client_id:
            query["client_id"] = self.client_id
        sep = "&" if "?" in self.endpoint else "?"
        r = send(self.transport, Request("GET", f"{self.endpoint}{sep}{urllib.parse.urlencode(query)}",
                                         {"X-IDENTITY-HEADER": self._header}), self.retry_delays, self.sleep)
        if r.response is None or not r.response.ok:
            raise RuntimeError("azure-blob: managed identity token failed: "
                               + (describe(r.response) if r.response is not None else str(r.error)))
        reply = r.response.json()
        if not isinstance(reply, dict) or not reply.get("access_token"):
            raise RuntimeError("azure-blob: managed identity reply has no access_token")
        return str(reply["access_token"]), _expiry(reply, self._now())


class ClientAssertionCredential(_CachedToken):
    """Microsoft Entra ID client credentials with a federated client assertion (a workload identity
    token, or any OIDC token the app registration's federated credential trusts)::

        POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
             grant_type=client_credentials, client_id, scope=https://storage.azure.com/.default,
             client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer, client_assertion
    """

    def __init__(self, tenant_id: str, client_id: str, assertion: Callable[[], str], *,
                 authority: str = AZURE_AUTHORITY, transport: Transport | None = None,
                 retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.time) -> None:
        super().__init__(now)
        if not tenant_id or not client_id:
            raise ValueError("client assertion: REDBERRY_AZURE_TENANT_ID and REDBERRY_AZURE_CLIENT_ID (or AZURE_TENANT_ID "
                             "and AZURE_CLIENT_ID) are required")
        self.tenant_id, self.client_id, self.assertion = tenant_id, client_id, assertion
        self.authority = trim_base(authority)
        self.transport = transport or urllib_transport
        self.retry_delays, self.sleep = retry_delays, sleep

    def _mint(self) -> tuple[str, float]:
        form = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": self.client_id, "scope": AZURE_STORAGE_SCOPE,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": self.assertion(),
        }).encode()
        url = f"{self.authority}/{urllib.parse.quote(self.tenant_id, safe='')}/oauth2/v2.0/token"
        r = send(self.transport, Request("POST", url, {"content-type": "application/x-www-form-urlencoded"}, form),
                 self.retry_delays, self.sleep)
        if r.response is None or not r.response.ok:
            raise RuntimeError("azure-blob: Entra ID token exchange failed: "
                               + (describe(r.response) if r.response is not None else str(r.error)))
        reply = r.response.json()
        if not isinstance(reply, dict) or not reply.get("access_token"):
            raise RuntimeError("azure-blob: Entra ID reply has no access_token")
        return str(reply["access_token"]), _expiry(reply, self._now())


def _expiry(reply: Mapping[str, object], now: float) -> float:
    for key, relative in (("expires_on", False), ("expires_in", True)):
        try:
            value = float(str(reply[key]))
        except (KeyError, TypeError, ValueError):
            continue
        return now + value if relative else value
    return now + 3600


def _read_file(path: str) -> Callable[[], str]:
    def read() -> str:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return read


def _first(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def credential_from_env(env: Mapping[str, str] | None = None, *, transport: Transport | None = None,
                        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS) -> TokenCredential:
    """The managed identity when ``IDENTITY_ENDPOINT`` and ``IDENTITY_HEADER`` are set (Azure Functions, App
    Service), else a client assertion read from ``AZURE_FEDERATED_TOKEN_FILE``.

    The client id (a user-assigned identity; none: the system-assigned one) is ``REDBERRY_AZURE_CLIENT_ID``,
    else ``AZURE_CLIENT_ID``, and the tenant ``REDBERRY_AZURE_TENANT_ID``, else ``AZURE_TENANT_ID``. The
    ``REDBERRY_`` names come first so the hasher's identity need not be set app-wide, where
    ``AZURE_CLIENT_ID`` would also change the app's own ``DefaultAzureCredential``."""
    env = os.environ if env is None else env
    client_id = _first(env, "REDBERRY_AZURE_CLIENT_ID", "AZURE_CLIENT_ID")
    if env.get("IDENTITY_ENDPOINT") and env.get("IDENTITY_HEADER"):
        return ManagedIdentityCredential(client_id, endpoint=env["IDENTITY_ENDPOINT"], header=env["IDENTITY_HEADER"],
                                         transport=transport, retry_delays=retry_delays)
    token_file = (env.get("AZURE_FEDERATED_TOKEN_FILE") or "").strip()
    if token_file:
        return ClientAssertionCredential(_first(env, "REDBERRY_AZURE_TENANT_ID", "AZURE_TENANT_ID") or "", client_id or "",
                                         _read_file(token_file), transport=transport, retry_delays=retry_delays)
    raise ValueError("azure-blob: no credential: IDENTITY_ENDPOINT / IDENTITY_HEADER are not set (assign the managed "
                     "identity to the function app), and there is no AZURE_FEDERATED_TOKEN_FILE")


def _container_url(url: str) -> str:
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.netloc or parts.path.strip("/") == "":
        raise ValueError("REDBERRY_AZURE_CONTAINER_URL must be https://<account>.blob.core.windows.net/<container>")
    if parts.query or parts.fragment:
        raise ValueError("REDBERRY_AZURE_CONTAINER_URL must not carry a query (no SAS: the store uses the managed "
                         "identity)")
    return trim_base(urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))


class AzureBlobStore:
    kind = "azure-blob"

    def __init__(self, container_url: str, credential: TokenCredential, *, transport: Transport | None = None,
                 retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.container_url = _container_url(container_url)
        self.credential = credential
        self.transport = transport or urllib_transport
        self.retry_delays, self.sleep = retry_delays, sleep

    def put(self, path: str, data: bytes, meta: PutMeta) -> PutResult:
        url = f"{self.container_url}/{urllib.parse.quote(path, safe='/')}"
        headers = {
            "authorization": f"Bearer {self.credential.token()}",
            "x-ms-version": AZURE_BLOB_API_VERSION,
            "x-ms-date": formatdate(usegmt=True),
            "x-ms-blob-type": "BlockBlob",
            "if-none-match": "*",
            "content-type": "application/gzip",
            "x-ms-meta-sha256": meta.sha256,
        }
        r = send(self.transport, Request("PUT", url, headers, data), self.retry_delays, self.sleep)
        if r.response is None:
            raise RuntimeError(f"azure-blob: upload failed: {r.error}")
        if r.response.status in (409, 412):
            raise StoreConflictError(path, "azure-blob If-None-Match: *")
        if r.response.status == 401:
            self.credential.invalidate()
        if not r.response.ok:
            raise RuntimeError(f"azure-blob: upload failed: {describe(r.response)}")
        return PutResult(url=url)


# ── s3 ───────────────────────────────────────────────────────────────────────────────────────────

S3_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
STS_VERSION = "2011-06-15"


class AwsCredentialProvider(Protocol):
    def credentials(self) -> Credentials: ...

    def invalidate(self) -> None: ...


class EnvAwsCredentials:
    """``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY`` and ``AWS_SESSION_TOKEN``, read on each use (on
    Lambda, the execution role's, which the runtime keeps current)."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    def credentials(self) -> Credentials:
        env = os.environ if self._env is None else self._env
        key, secret = (env.get("AWS_ACCESS_KEY_ID") or "").strip(), (env.get("AWS_SECRET_ACCESS_KEY") or "").strip()
        if not key or not secret:
            raise RuntimeError("s3: no AWS credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not set)")
        return Credentials(key, secret, (env.get("AWS_SESSION_TOKEN") or "").strip() or None)

    def invalidate(self) -> None:
        pass


class WebIdentityCredentials:
    """A role's temporary credentials for a web identity token (an OIDC token: Vercel's, or a Kubernetes
    service account's ``AWS_WEB_IDENTITY_TOKEN_FILE``), from STS ``AssumeRoleWithWebIdentity`` (unsigned),
    used until five minutes before they expire::

        GET https://sts.<region>.amazonaws.com/?Action=AssumeRoleWithWebIdentity&Version=2011-06-15
            &RoleArn=<arn>&RoleSessionName=redberry&WebIdentityToken=<token>
    """

    def __init__(self, role_arn: str, token: Callable[[], str], region: str, *, session_name: str = "redberry",
                 transport: Transport | None = None, retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time) -> None:
        if not role_arn.startswith("arn:"):
            raise ValueError(f"s3: the role to assume is not an ARN: {role_arn!r}")
        self.role_arn, self.token, self.region, self.session_name = role_arn, token, region, session_name
        self.transport = transport or urllib_transport
        self.retry_delays, self.sleep, self._now = retry_delays, sleep, now
        self._lock = threading.Lock()
        self._creds: Credentials | None = None

    def credentials(self) -> Credentials:
        with self._lock:
            c = self._creds
            if c is None or (c.expires_at or 0) - TOKEN_REFRESH_MARGIN <= self._now():
                self._creds = c = self._assume()
            return c

    def invalidate(self) -> None:
        with self._lock:
            self._creds = None

    def _assume(self) -> Credentials:
        query = urllib.parse.urlencode({"Action": "AssumeRoleWithWebIdentity", "Version": STS_VERSION,
                                        "RoleArn": self.role_arn, "RoleSessionName": self.session_name,
                                        "WebIdentityToken": self.token()})
        host = f"sts.{self.region}.amazonaws.com" if self.region else "sts.amazonaws.com"
        r = send(self.transport, Request("GET", f"https://{host}/?{query}", {"accept": "application/xml"}),
                 self.retry_delays, self.sleep)
        if r.response is None or not r.response.ok:
            # STS echoes nothing secret, but keep the reply short.
            raise RuntimeError("s3: AssumeRoleWithWebIdentity failed: "
                               + (describe(r.response) if r.response is not None else str(r.error)))
        text = r.response.body.decode("utf-8", "replace")
        found = {k: _xml_value(text, k) for k in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")}
        if not (found["AccessKeyId"] and found["SecretAccessKey"] and found["SessionToken"]):
            raise RuntimeError("s3: the AssumeRoleWithWebIdentity reply has no credentials")
        return Credentials(found["AccessKeyId"], found["SecretAccessKey"], found["SessionToken"],
                           _iso_time(found["Expiration"]) or self._now() + 3600)


def _xml_value(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", text)
    return m.group(1).strip() if m else None


def _iso_time(value: str | None) -> float | None:
    if not value:
        return None
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def aws_credentials_from_env(env: Mapping[str, str] | None = None, *, region: str = "",
                             transport: Transport | None = None,
                             retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS) -> AwsCredentialProvider:
    """A role assumed with a web identity token when ``REDBERRY_AWS_ROLE_ARN`` (or ``AWS_ROLE_ARN``) is set
    with a token (``AWS_WEB_IDENTITY_TOKEN_FILE``, else ``VERCEL_OIDC_TOKEN``), else the standard
    environment's keys (``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``AWS_SESSION_TOKEN``)."""
    env = os.environ if env is None else env
    role = _first(env, "REDBERRY_AWS_ROLE_ARN", "AWS_ROLE_ARN")
    token_file = _first(env, "AWS_WEB_IDENTITY_TOKEN_FILE")
    if role and (token_file or env.get("VERCEL_OIDC_TOKEN")):
        token = _read_file(token_file) if token_file else (lambda: (os.environ.get("VERCEL_OIDC_TOKEN")
                                                                     or env.get("VERCEL_OIDC_TOKEN") or "").strip())
        return WebIdentityCredentials(role, token, region, transport=transport, retry_delays=retry_delays)
    if not (_first(env, "AWS_ACCESS_KEY_ID") and _first(env, "AWS_SECRET_ACCESS_KEY")):
        if role:
            raise ValueError("s3: REDBERRY_AWS_ROLE_ARN is set but there is no web identity token "
                             "(AWS_WEB_IDENTITY_TOKEN_FILE or VERCEL_OIDC_TOKEN)")
        raise ValueError("s3: no AWS credentials: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not set (on Lambda "
                         "they are the execution role's), and no REDBERRY_AWS_ROLE_ARN with a web identity token")
    return EnvAwsCredentials(None if env is os.environ else env)


def s3_bucket(value: str) -> str:
    """``<bucket>`` or ``s3://<bucket>`` as the bucket's name."""
    name = (value or "").strip().removeprefix("s3://").strip("/")
    if not S3_BUCKET.fullmatch(name) or ".." in name:
        raise ValueError("REDBERRY_S3_BUCKET must be an S3 bucket name or s3://<bucket>")
    return name


class S3Store:
    kind = "s3"

    def __init__(self, bucket: str, region: str, credentials: AwsCredentialProvider, *,
                 transport: Transport | None = None, retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
                 sleep: Callable[[float], None] = time.sleep, endpoint: str | None = None) -> None:
        self.bucket = s3_bucket(bucket)
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region or ""):
            raise ValueError("REDBERRY_S3_REGION (or AWS_REGION) must be the bucket's region, e.g. eu-west-1")
        self.region, self.credentials = region, credentials
        self.transport = transport or urllib_transport
        self.retry_delays, self.sleep = retry_delays, sleep
        # Virtual-hosted style; a bucket with dots does not match the wildcard certificate: path style.
        self.endpoint = trim_base(endpoint) if endpoint else (
            f"https://s3.{region}.amazonaws.com/{self.bucket}" if "." in self.bucket
            else f"https://{self.bucket}.s3.{region}.amazonaws.com")

    def put(self, path: str, data: bytes, meta: PutMeta) -> PutResult:
        url = f"{self.endpoint}/{urllib.parse.quote(path, safe='/')}"
        digest = hashlib.sha256(data).digest()
        headers = {
            "content-type": "application/gzip",
            "if-none-match": "*",
            "x-amz-content-sha256": digest.hex(),
            "x-amz-checksum-sha256": base64.b64encode(digest).decode(),
            "content-md5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode(),
            "x-amz-meta-sha256": meta.sha256,
        }

        def once() -> Response:
            # Signed per attempt: a retry after a wait still has a fresh x-amz-date.
            signed = sign("PUT", url, headers, data, self.credentials.credentials(), self.region, "s3",
                          payload_sha256=headers["x-amz-content-sha256"])
            return _suppressed(self.transport, Request("PUT", url, signed, data))

        r = with_retries(once, self.retry_delays, self.sleep)
        if r.response is None:
            raise RuntimeError(f"s3: upload failed: {r.error}")
        if r.response.status in (409, 412):
            raise StoreConflictError(path, "s3 If-None-Match: *")
        if r.response.status in (400, 403) and re.search(rb"ExpiredToken|InvalidToken|TokenRefreshRequired",
                                                         r.response.body or b""):
            self.credentials.invalidate()
        if not r.response.ok:
            raise RuntimeError(f"s3: upload failed: {describe(r.response)}")
        return PutResult(url=f"s3://{self.bucket}/{path}")


# ── from the environment ─────────────────────────────────────────────────────────────────────────


def store_from_env(env: Mapping[str, str] | None = None, *, url: str | None = None, token: str | None = None,
                   transport: Transport | None = None,
                   retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS) -> Store:
    """The store ``REDBERRY_STORE`` names: ``redberry`` (the default, as in the CLI), ``azure-blob`` (with
    ``REDBERRY_AZURE_CONTAINER_URL`` and a credential, ``credential_from_env``) or ``s3`` (with
    ``REDBERRY_S3_BUCKET``, ``REDBERRY_S3_REGION`` or ``AWS_REGION``, and ``aws_credentials_from_env``)."""
    env = os.environ if env is None else env
    kind = (env.get("REDBERRY_STORE") or "redberry").strip()
    if kind == "redberry":
        return RedberryStore(url, token, transport=transport, retry_delays=retry_delays)
    if kind == "azure-blob":
        return AzureBlobStore(env.get("REDBERRY_AZURE_CONTAINER_URL") or "",
                              credential_from_env(env, transport=transport, retry_delays=retry_delays),
                              transport=transport, retry_delays=retry_delays)
    if kind == "s3":
        if not (env.get("REDBERRY_S3_BUCKET") or "").strip():
            raise ValueError("REDBERRY_STORE=s3 needs REDBERRY_S3_BUCKET (s3://<bucket>)")
        region = _first(env, "REDBERRY_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION") or ""
        if not region:
            raise ValueError("REDBERRY_STORE=s3 needs the bucket's region: REDBERRY_S3_REGION (or AWS_REGION)")
        return S3Store(env.get("REDBERRY_S3_BUCKET") or "", region,
                       aws_credentials_from_env(env, region=region, transport=transport, retry_delays=retry_delays),
                       transport=transport, retry_delays=retry_delays)
    if kind in ("vercel-blob", "gcs"):
        raise ValueError(f"REDBERRY_STORE={kind} is for @redberrylabs/otel on Vercel; the Python hasher stores to "
                         "redberry, azure-blob or s3")
    raise ValueError(f"REDBERRY_STORE must be redberry, azure-blob or s3, not {kind!r}")
