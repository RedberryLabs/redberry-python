"""The emit path on AWS: where a named agent's spans go, and how each export is signed.

Used by ``redberry_sdk._emit`` when the agent runs on AWS (``AWS_LAMBDA_FUNCTION_NAME``, ECS's
``ECS_CONTAINER_METADATA_URI_V4``, or ``REDBERRY_CLOUD=aws``), is named, and no endpoint is given:

1. Credentials: the environment's (``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``/``AWS_SESSION_TOKEN``,
   on Lambda the execution role's, read on each use), else the ECS container credentials endpoint
   (``AWS_CONTAINER_CREDENTIALS_FULL_URI`` or ``..._RELATIVE_URI``, with ``AWS_CONTAINER_AUTHORIZATION_TOKEN``
   or ``..._TOKEN_FILE``), refreshed five minutes before they expire.
2. The account: SigV4-signed ``sts:GetCallerIdentity`` on the regional STS endpoint (Query API, XML).
3. The region: ``AWS_REGION``, else ``AWS_DEFAULT_REGION``.
4. ``GET https://redberry-<acct>-<region>.s3.<region>.amazonaws.com/ingest.json``, SigV4-signed (``s3``),
   with ``x-amz-expected-bucket-owner: <acct>`` (S3 refuses with 403 if another account owns the
   bucket), giving ``{"url": ..., "region": ...}``: the ingest function URL.

Each OTLP POST to that URL is SigV4-signed for ``lambda`` (``x-amz-content-sha256`` = the body's
SHA-256, ``x-amz-security-token`` with temporary credentials). The grants are on Redberry's resources
(the bucket policy and the function URL's resource policy); the agent's role needs nothing attached.

Standard library and ``requests`` only: no AWS SDK.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

import requests

_log = logging.getLogger("redberry.sdk")

CLOUD_ENV = "REDBERRY_CLOUD"
ECS_LOCAL_HOST = "http://169.254.170.2"
STS_VERSION = "2011-06-15"
TIMEOUT_S = 3.0
REFRESH_MARGIN_S = 300.0

#: ``transport(method, url, headers, body) -> (status, headers, body)``; raises OSError when unreachable.
Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Mapping[str, str], bytes]]


class AwsDiscoveryError(RuntimeError):
    pass


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None
                     ) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() or b""


def on_aws(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    if (env.get(CLOUD_ENV) or "").strip().lower() == "aws":
        return True
    return bool((env.get("AWS_LAMBDA_FUNCTION_NAME") or "").strip()
                or (env.get("ECS_CONTAINER_METADATA_URI_V4") or "").strip())


def region_from_env(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip()


def _sigv4():
    from .hasher import _sigv4  # lazily: only the AWS path needs it
    return _sigv4


# ── credentials ──────────────────────────────────────────────────────────────────────────────────

class AwsCredentials:
    """The runtime's credentials: the environment's when set, else the ECS container endpoint's
    (cached until five minutes before ``Expiration``)."""

    def __init__(self, env: Mapping[str, str] | None = None, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._env = env
        self._transport = transport or urllib_transport
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Any = None

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def credentials(self):
        Credentials = _sigv4().Credentials
        env = self.env
        key = (env.get("AWS_ACCESS_KEY_ID") or "").strip()
        secret = (env.get("AWS_SECRET_ACCESS_KEY") or "").strip()
        if key and secret:
            return Credentials(key, secret, (env.get("AWS_SESSION_TOKEN") or "").strip() or None)
        with self._lock:
            c = self._cached
            if c is None or (c.expires_at is not None and c.expires_at - REFRESH_MARGIN_S <= self._clock()):
                self._cached = c = self._from_container()
            return c

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def _container_url(self) -> str:
        env = self.env
        full = (env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI") or "").strip()
        if full:
            return full
        relative = (env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or "").strip()
        if relative:
            return ECS_LOCAL_HOST + (relative if relative.startswith("/") else "/" + relative)
        raise AwsDiscoveryError("no AWS credentials: AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY and "
                                "AWS_CONTAINER_CREDENTIALS_*_URI are not set")

    def _auth_token(self) -> str | None:
        env = self.env
        path = (env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE") or "").strip()
        if path:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        return (env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN") or "").strip() or None

    def _from_container(self):
        sigv4 = _sigv4()
        headers = {"Accept": "application/json"}
        token = self._auth_token()
        if token:
            headers["Authorization"] = token
        status, _, body = self._transport("GET", self._container_url(), headers, None)
        if status != 200:
            raise AwsDiscoveryError(f"the container credentials endpoint answered {status}")
        data = json.loads(body)
        expires = data.get("Expiration")
        expires_at = None
        if expires:
            import datetime as _dt
            try:
                expires_at = _dt.datetime.fromisoformat(expires).timestamp()
            except ValueError:
                expires_at = self._clock() + 900.0
        return sigv4.Credentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("Token") or None,
                                 expires_at)


# ── discovery ────────────────────────────────────────────────────────────────────────────────────

def _xml_value(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", text)
    return m.group(1).strip() if m else None


def caller_account(creds: AwsCredentials, region: str, transport: Transport) -> str:
    """The account id, from a SigV4-signed ``sts:GetCallerIdentity`` (POST, Query API)."""
    url = f"https://sts.{region}.amazonaws.com/"
    body = urllib.parse.urlencode({"Action": "GetCallerIdentity", "Version": STS_VERSION}).encode()
    headers = _sigv4().sign("POST", url, {"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
                            body, creds.credentials(), region, "sts")
    status, _, reply = transport("POST", url, headers, body)
    if status != 200:
        raise AwsDiscoveryError(f"sts:GetCallerIdentity answered {status}")
    account = _xml_value(reply.decode("utf-8", "replace"), "Account")
    if not account or not account.isdigit():
        raise AwsDiscoveryError("sts:GetCallerIdentity reply has no account")
    return account


def ingest_config_url(account: str, region: str) -> str:
    return f"https://redberry-{account}-{region}.s3.{region}.amazonaws.com/ingest.json"


def read_ingest_config(creds: AwsCredentials, account: str, region: str, transport: Transport) -> dict:
    """``ingest.json`` from the installation's bucket, only if ``account`` owns the bucket."""
    sigv4 = _sigv4()
    url = ingest_config_url(account, region)
    headers = sigv4.sign("GET", url, {"x-amz-content-sha256": sigv4.EMPTY_SHA256,
                                      "x-amz-expected-bucket-owner": account},
                         None, creds.credentials(), region, "s3", payload_sha256=sigv4.EMPTY_SHA256)
    status, _, body = transport("GET", url, headers, None)
    if status != 200:
        raise AwsDiscoveryError(f"s3://redberry-{account}-{region}/ingest.json answered {status}"
                                + (" (bucket not owned by this account, or no access)" if status == 403 else ""))
    data = json.loads(body)
    url_ = str(data.get("url") or "").rstrip("/")
    if not url_.startswith("https://"):
        raise AwsDiscoveryError("ingest.json has no https url")
    return {"url": url_, "region": str(data.get("region") or region)}


def discover(env: Mapping[str, str] | None = None, transport: Transport | None = None
             ) -> tuple[str, str, AwsCredentials]:
    """(ingest URL, its region, the credentials provider). Raises AwsDiscoveryError (or OSError)."""
    transport = transport or urllib_transport
    region = region_from_env(env)
    if not region:
        raise AwsDiscoveryError("AWS_REGION is not set")
    creds = AwsCredentials(env, transport)
    account = caller_account(creds, region, transport)
    config = read_ingest_config(creds, account, region, transport)
    return config["url"], config["region"], creds


# ── auth on each export ──────────────────────────────────────────────────────────────────────────

class SigV4Auth(requests.auth.AuthBase):
    """Signs each export (service ``lambda``) with the runtime's credentials. A credentials failure
    is raised as ``_emit.TokenUnavailable`` (a ConnectionError: retried by the exporter)."""

    def __init__(self, credentials: AwsCredentials, region: str, service: str = "lambda") -> None:
        self.creds = credentials
        self.region = region
        self.service = service
        self._failed_logged = False

    def invalidate(self) -> None:
        self.creds.invalidate()

    def __call__(self, request):
        from ._emit import TokenUnavailable
        sigv4 = _sigv4()
        body = request.body or b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            credentials = self.creds.credentials()
        except Exception as e:
            if not self._failed_logged:
                self._failed_logged = True
                _log.warning("redberry_sdk: no AWS credentials to sign the export (%s); spans stay queued "
                             "and the export is retried", e)
            raise TokenUnavailable(str(e)) from e
        self._failed_logged = False
        payload = hashlib.sha256(body).hexdigest()
        keep = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "content-encoding")}
        keep["x-amz-content-sha256"] = payload
        signed = sigv4.sign(request.method, request.url, keep, body, credentials, self.region, self.service,
                            payload_sha256=payload)
        for name, value in signed.items():
            if name != "host":
                request.headers[name] = value
        return request
