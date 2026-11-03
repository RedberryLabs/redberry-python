"""The emit path on Azure (redberry_sdk._emit_azure): managed identity tokens, discovery via the
Redberry API, Bearer on every export, resource attributes. No Azure call: a fake transport answers."""
from __future__ import annotations

import json
import urllib.parse

import pytest
import requests

from redberry_sdk import _emit, _emit_aws, _emit_azure

INGEST = "https://redberry-ingest.example.azurecontainerapps.io"
APP = _emit_azure.DEFAULT_APP_ID_URI
AZ_VARS = ("REDBERRY_CLOUD", "IDENTITY_ENDPOINT", "IDENTITY_HEADER", "AZURE_CLIENT_ID", "WEBSITE_SITE_NAME",
           "CONTAINER_APP_NAME", "FUNCTIONS_WORKER_RUNTIME", "REGION_NAME", "WEBSITE_REGION",
           "WEBSITE_OWNER_NAME", "WEBSITE_RESOURCE_GROUP", "AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP",
           "CONTAINER_APP_REGION", "REDBERRY_AZURE_APP_ID_URI", "REDBERRY_API_URL", "K_SERVICE",
           "AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4")
FUNC_ENV = {"IDENTITY_ENDPOINT": "http://localhost:8081/msi/token", "IDENTITY_HEADER": "secret-h",
            "WEBSITE_SITE_NAME": "refund-agent", "FUNCTIONS_WORKER_RUNTIME": "python"}


class FakeAzure:
    def __init__(self, discover_status=200, expires_on=10_000):
        self.calls: list[tuple[str, str, dict]] = []
        self.discover_status = discover_status
        self.expires_on = expires_on
        self.n = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        if "/api/discover/azure" in url:
            return self.discover_status, {}, json.dumps({"ingest_url": INGEST + "/",
                                                         "installation_id": "i"}).encode()
        self.n += 1
        return 200, {}, json.dumps({"access_token": f"tok{self.n}",
                                    "expires_on": str(self.expires_on)}).encode()


def _q(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in (_emit.ENDPOINT_ENV, _emit.INGEST_URL_ENV, *AZ_VARS):
        monkeypatch.delenv(name, raising=False)
    _emit._reset_for_tests()
    yield
    _emit._reset_for_tests()


def test_on_azure_detection():
    assert not _emit_azure.on_azure({})
    assert _emit_azure.on_azure({"REDBERRY_CLOUD": "Azure"})
    assert _emit_azure.on_azure({"CONTAINER_APP_NAME": "a"})
    assert _emit_azure.on_azure({"WEBSITE_SITE_NAME": "a"})
    assert _emit_azure.on_azure({"IDENTITY_ENDPOINT": "x", "IDENTITY_HEADER": "y"})
    assert not _emit_azure.on_azure({"IDENTITY_ENDPOINT": "x"})


def test_app_service_token_request():
    fake = FakeAzure()
    _emit_azure.ManagedIdentityToken(APP, FUNC_ENV, fake).token()
    _, url, headers = fake.calls[0]
    assert url.startswith("http://localhost:8081/msi/token?")
    assert _q(url) == {"api-version": "2019-08-01", "resource": APP}
    assert headers == {"X-IDENTITY-HEADER": "secret-h"}


def test_user_assigned_client_id():
    fake = FakeAzure()
    _emit_azure.ManagedIdentityToken(APP, {**FUNC_ENV, "AZURE_CLIENT_ID": "cid"}, fake).token()
    assert _q(fake.calls[0][1])["client_id"] == "cid"


def test_imds_token_request():
    fake = FakeAzure()
    _emit_azure.ManagedIdentityToken(APP, {"REDBERRY_CLOUD": "azure"}, fake).token()
    _, url, headers = fake.calls[0]
    assert url.startswith("http://169.254.169.254/metadata/identity/oauth2/token?")
    assert _q(url) == {"api-version": "2018-02-01", "resource": APP}
    assert headers == {"Metadata": "true"}


def test_token_cached_until_five_minutes_before_expiry():
    now = [1000.0]
    fake = FakeAzure(expires_on=5000)
    t = _emit_azure.ManagedIdentityToken(APP, FUNC_ENV, fake, clock=lambda: now[0])
    assert t.token() == t.token() == "tok1"
    now[0] = 4699.0
    assert t.token() == "tok1"
    now[0] = 4700.0
    assert t.token() == "tok2"
    t.invalidate()
    assert t.token() == "tok3"


def test_discovery_with_bearer_and_app_id_override():
    fake = FakeAzure()
    env = {**FUNC_ENV, "REDBERRY_AZURE_APP_ID_URI": "api://other", "REDBERRY_API_URL": "https://api.x/"}
    url, _ = _emit_azure.discover(env, fake)
    assert url == INGEST
    assert _q(fake.calls[0][1])["resource"] == "api://other"
    _, durl, dheaders = fake.calls[1]
    assert durl == "https://api.x/api/discover/azure"
    assert dheaders["Authorization"] == "Bearer tok1"


def test_default_api_url():
    fake = FakeAzure()
    _emit_azure.discover(FUNC_ENV, fake)
    assert fake.calls[1][1] == "https://api-dev.redberry.sbs/api/discover/azure"


def test_resolve_endpoint_discovers_on_azure_and_caches(monkeypatch):
    for k, v in FUNC_ENV.items():
        monkeypatch.setenv(k, v)
    fake = FakeAzure()
    monkeypatch.setattr(_emit_azure, "urllib_transport", fake)
    monkeypatch.setattr(_emit, "discover_gcp", lambda: pytest.fail("GCP probed on Azure"))
    ep = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    assert (ep.url, ep.source) == (INGEST, "azure")
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True) is ep
    assert len(fake.calls) == 2


def test_discovery_failure_falls_back(monkeypatch, caplog):
    for k, v in FUNC_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(_emit_azure, "urllib_transport", FakeAzure(discover_status=404))
    monkeypatch.setattr(_emit, "discover_gcp", lambda: None)
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True).source == "default"
    assert "could not be discovered" in caplog.text


def test_ingest_url_override_skips_discovery(monkeypatch):
    for k, v in FUNC_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(_emit.INGEST_URL_ENV, "https://ingest.example/")
    monkeypatch.setattr(_emit_azure, "urllib_transport", lambda *a: pytest.fail("discovery ran"))
    ep = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    assert (ep.url, ep.source) == ("https://ingest.example", "explicit")
    assert _emit.build_session(ep) is None


def test_export_carries_bearer_and_401_refreshes():
    fake = FakeAzure()
    tokens = _emit_azure.ManagedIdentityToken(APP, FUNC_ENV, fake)
    session = _emit.build_session(_emit.Endpoint(url=INGEST, source="azure", azure_token=tokens))
    assert isinstance(session.auth, _emit_azure.BearerAuth)
    req = requests.Request("POST", INGEST + "/v1/traces", data=b"x")
    assert session.prepare_request(req).headers["Authorization"] == "Bearer tok1"
    session.auth.invalidate()  # what the retry wrapper does on 401
    assert session.prepare_request(req).headers["Authorization"] == "Bearer tok2"


def test_export_without_token_is_transient():
    def down(*a):
        raise OSError("unreachable")
    auth = _emit_azure.BearerAuth(_emit_azure.ManagedIdentityToken(APP, FUNC_ENV, down))
    with pytest.raises(_emit.TokenUnavailable):
        auth(requests.Request("POST", INGEST, data=b"x").prepare())


def test_processor_keeps_auth_retry_for_azure():
    tokens = _emit_azure.ManagedIdentityToken(APP, FUNC_ENV, FakeAzure())
    processor = _emit.build_processor(_emit.Endpoint(url=INGEST, source="azure", azure_token=tokens))
    try:
        assert isinstance(processor._auth_retry._auth, _emit_azure.BearerAuth)
    finally:
        processor.shutdown()


def _attrs(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return _emit.cloud_run_resource_attributes()


def test_functions_resource_attributes(monkeypatch):
    assert _attrs(monkeypatch, {**FUNC_ENV, "REGION_NAME": "West Europe",
                                "WEBSITE_OWNER_NAME": "sub-1+rb-rg-WestEuropewebspace-Linux",
                                "WEBSITE_RESOURCE_GROUP": "rb-rg"}) == {
        "cloud.provider": "azure", "cloud.platform": "azure_functions", "faas.name": "refund-agent",
        "cloud.region": "West Europe",
        "cloud.resource_id": "/subscriptions/sub-1/resourceGroups/rb-rg/providers/Microsoft.Web/sites/refund-agent",
    }


def test_app_service_resource_attributes_without_rg(monkeypatch):
    assert _attrs(monkeypatch, {"WEBSITE_SITE_NAME": "web", "WEBSITE_OWNER_NAME": "sub+rg-x"}) == {
        "cloud.provider": "azure", "cloud.platform": "azure_app_service", "faas.name": "web"}


def test_container_apps_resource_attributes(monkeypatch):
    assert _attrs(monkeypatch, {"CONTAINER_APP_NAME": "agent", "AZURE_SUBSCRIPTION_ID": "s",
                                "AZURE_RESOURCE_GROUP": "g"}) == {
        "cloud.provider": "azure", "cloud.platform": "azure_container_apps", "faas.name": "agent",
        "cloud.resource_id": "/subscriptions/s/resourceGroups/g/providers/Microsoft.App/containerApps/agent"}


def test_gcp_and_aws_unchanged(monkeypatch):
    gcp = _emit.Endpoint(url="https://redberry-ingest-1.r.run.app", source="gcp", audience="a")
    monkeypatch.setattr(_emit, "discover_gcp", lambda: gcp)
    monkeypatch.setattr(_emit_azure, "urllib_transport", lambda *a: pytest.fail("Azure probed on GCP"))
    monkeypatch.setattr(_emit_aws, "urllib_transport", lambda *a: pytest.fail("AWS probed on GCP"))
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True) is gcp
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "f")
    assert _emit.cloud_run_resource_attributes()["cloud.provider"] == "aws"
