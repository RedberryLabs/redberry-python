"""The emit path on Azure: where a named agent's spans go, and how each export authenticates.

Used by ``redberry_sdk._emit`` when the agent runs on Azure (``IDENTITY_ENDPOINT`` + ``IDENTITY_HEADER``,
``WEBSITE_SITE_NAME``, ``CONTAINER_APP_NAME``, or ``REDBERRY_CLOUD=azure``), is named, and no endpoint
is given:

1. A token from the agent's managed identity for Redberry's Entra app (``api://<app id>``; the
   environment's constant, overridable with ``REDBERRY_AZURE_APP_ID_URI``). App Service, Functions and
   Container Apps: ``IDENTITY_ENDPOINT`` with ``X-IDENTITY-HEADER``; VMs and AKS: IMDS with
   ``Metadata: true``. ``AZURE_CLIENT_ID`` selects a user-assigned identity. Cached until five minutes
   before it expires.
2. ``GET <REDBERRY_API_URL>/api/discover/azure`` with ``Authorization: Bearer <token>`` gives
   ``{"ingest_url": ...}``.

Each OTLP POST carries the same token as ``Authorization: Bearer``. Standard library and ``requests``
only: no Azure SDK.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping

import requests

from ._emit_aws import Transport, urllib_transport  # the shared stdlib transport

_log = logging.getLogger("redberry.sdk")

CLOUD_ENV = "REDBERRY_CLOUD"
APP_ID_URI_ENV = "REDBERRY_AZURE_APP_ID_URI"
API_URL_ENV = "REDBERRY_API_URL"
DEFAULT_APP_ID_URI = "api://c1b7f6d8-a898-4679-8343-9223a5f832d7"  # Redberry Ingest (dev)
DEFAULT_API_URL = "https://api-dev.redberry.sbs"
IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
REFRESH_MARGIN_S = 300.0


class AzureDiscoveryError(RuntimeError):
    pass


def _get(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def on_azure(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    if _get(env, CLOUD_ENV).lower() == "azure":
        return True
    return bool((_get(env, "IDENTITY_ENDPOINT") and _get(env, "IDENTITY_HEADER"))
                or _get(env, "WEBSITE_SITE_NAME") or _get(env, "CONTAINER_APP_NAME"))


def app_id_uri(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return _get(env, APP_ID_URI_ENV) or DEFAULT_APP_ID_URI


def api_url(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (_get(env, API_URL_ENV) or DEFAULT_API_URL).rstrip("/")


class ManagedIdentityToken:
    """The managed identity's access token for ``resource``, cached until five minutes before expiry."""

    def __init__(self, resource: str, env: Mapping[str, str] | None = None,
                 transport: Transport | None = None, clock: Callable[[], float] = time.time) -> None:
        self.resource = resource
        self._env = env
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._refresh_at = 0.0

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def _request(self) -> tuple[str, dict[str, str]]:
        env = self.env
        endpoint, secret = _get(env, "IDENTITY_ENDPOINT"), _get(env, "IDENTITY_HEADER")
        client_id = _get(env, "AZURE_CLIENT_ID")
        if endpoint and secret:
            query = {"api-version": "2019-08-01", "resource": self.resource}
            if client_id:
                query["client_id"] = client_id
            sep = "&" if "?" in endpoint else "?"
            return endpoint + sep + urllib.parse.urlencode(query), {"X-IDENTITY-HEADER": secret}
        query = {"api-version": "2018-02-01", "resource": self.resource}
        if client_id:
            query["client_id"] = client_id
        return IMDS_TOKEN_URL + "?" + urllib.parse.urlencode(query), {"Metadata": "true"}

    def token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._refresh_at:
                return self._token
            url, headers = self._request()
            transport = self._transport or urllib_transport
            status, _, body = transport("GET", url, headers, None)
            if status != 200:
                raise AzureDiscoveryError(f"the managed identity endpoint answered {status}")
            data = json.loads(body)
            token = str(data.get("access_token") or "")
            if not token:
                raise AzureDiscoveryError("the managed identity endpoint gave no access_token")
            try:
                expires_at = float(data["expires_on"])
            except (KeyError, TypeError, ValueError):
                try:
                    expires_at = now + float(data["expires_in"])
                except (KeyError, TypeError, ValueError):
                    expires_at = now + 1800.0 + REFRESH_MARGIN_S
            self._token = token
            self._refresh_at = expires_at - REFRESH_MARGIN_S
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._refresh_at = 0.0


def discover(env: Mapping[str, str] | None = None, transport: Transport | None = None
             ) -> tuple[str, ManagedIdentityToken]:
    """(ingest URL, the token provider). Raises AzureDiscoveryError (or OSError)."""
    transport = transport or urllib_transport
    tokens = ManagedIdentityToken(app_id_uri(env), env, transport)
    url = api_url(env) + "/api/discover/azure"
    status, _, body = transport("GET", url, {"Authorization": f"Bearer {tokens.token()}",
                                             "Accept": "application/json"}, None)
    if status != 200:
        raise AzureDiscoveryError(f"{url} answered {status}"
                                  + (" (this identity is not registered with an installation)"
                                     if status == 404 else ""))
    ingest = str(json.loads(body).get("ingest_url") or "").rstrip("/")
    if not ingest.startswith("https://"):
        raise AzureDiscoveryError("discovery gave no https ingest_url")
    return ingest, tokens


class BearerAuth(requests.auth.AuthBase):
    """``Authorization: Bearer <managed identity token>`` on each export. A token failure is raised
    as ``_emit.TokenUnavailable`` (retried by the exporter)."""

    def __init__(self, tokens: ManagedIdentityToken) -> None:
        self.tokens = tokens
        self._failed_logged = False

    def invalidate(self) -> None:
        self.tokens.invalidate()

    def __call__(self, request):
        from ._emit import TokenUnavailable
        try:
            token = self.tokens.token()
        except Exception as e:
            if not self._failed_logged:
                self._failed_logged = True
                _log.warning("redberry_sdk: no managed identity token for %s (%s); spans stay queued and "
                             "the export is retried", self.tokens.resource, e)
            raise TokenUnavailable(str(e)) from e
        self._failed_logged = False
        request.headers["Authorization"] = f"Bearer {token}"
        return request


def resource_attributes(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """OpenTelemetry attributes on Functions, Container Apps and App Service; empty elsewhere."""
    env = os.environ if env is None else env
    site, app = _get(env, "WEBSITE_SITE_NAME"), _get(env, "CONTAINER_APP_NAME")
    if _get(env, "FUNCTIONS_WORKER_RUNTIME") and (site or not app):
        platform = "azure_functions"
    elif app:
        platform = "azure_container_apps"
    elif site:
        platform = "azure_app_service"
    else:
        return {}
    name = app if platform == "azure_container_apps" else site
    attrs = {"cloud.provider": "azure", "cloud.platform": platform}
    if name:
        attrs["faas.name"] = name
    region = _get(env, "REGION_NAME") or _get(env, "WEBSITE_REGION") or _get(env, "CONTAINER_APP_REGION")
    if region:
        attrs["cloud.region"] = region
    rid = _resource_id(env, platform, name)
    if rid:
        attrs["cloud.resource_id"] = rid
    return attrs


def _resource_id(env: Mapping[str, str], platform: str, name: str) -> str | None:
    if not name:
        return None
    if platform == "azure_container_apps":
        sub, rg = _get(env, "AZURE_SUBSCRIPTION_ID"), _get(env, "AZURE_RESOURCE_GROUP")
        if sub and rg:
            return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.App/containerApps/{name}"
        return None
    owner = _get(env, "WEBSITE_OWNER_NAME")  # <subscription>+<rg>-<region>webspace[-Linux]
    sub = owner.split("+", 1)[0] if "+" in owner else ""
    rg = _get(env, "WEBSITE_RESOURCE_GROUP")  # not parsed from the owner name: an RG may contain '-'
    if sub and rg:
        return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Web/sites/{name}"
    return None
