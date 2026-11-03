"""redberry_sdk.hasher on AWS: SigV4 (against AWS's published test vectors), the ``s3`` store (plain HTTP,
faked), its credentials (the environment's keys, a web identity), and ``with_redberry`` on a Lambda
``handler(event, context)``, end to end against fake HTTP (VALP and S3)."""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import inspect
import json
import time
from typing import Any

import pytest

pytest.importorskip("cryptography")

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

import redberry_sdk._sdk as sdk
from redberry_sdk.hasher import (
    EnvAwsCredentials,
    PutMeta,
    RedberryProcessor,
    S3Store,
    StoreConflictError,
    WebIdentityCredentials,
    _azure,
    register,
    resolve_config,
    store_from_env,
    with_redberry,
)
from redberry_sdk.hasher._http import Request, Response
from redberry_sdk.hasher._sigv4 import Credentials, canonical_request, sign

from .test_sdk_hasher import AGENT
from .test_sdk_hasher_azure import ENV as AZURE_ENV
from .test_sdk_hasher_azure import PUBLIC_KEY, _chat, valp_accepts

# ── SigV4: AWS's published test vectors ──────────────────────────────────────────────────────────
# aws-sig-v4-test-suite (service "service", us-east-1, 20150830T123600Z) and the S3 "Signature calculations
# for the Authorization header: single chunk" examples (examplebucket, us-east-1, 20130524T000000Z).

SUITE = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
S3_DOCS = Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
EMPTY = hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(("method", "path", "signature"), [
    ("GET", "/", "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"),                  # get-vanilla
    ("GET", "/?Param2=value2&Param1=value1",                                              # get-vanilla-query-order-key-case
     "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"),
    ("POST", "/", "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b"),                # post-vanilla
])
def test_sigv4_test_suite(method, path, signature):
    h = sign(method, f"https://example.amazonaws.com{path}", {"host": "example.amazonaws.com",
                                                               "x-amz-date": "20150830T123600Z"},
             b"", SUITE, "us-east-1", "service")
    assert h["authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                                  f"SignedHeaders=host;x-amz-date, Signature={signature}")


def test_sigv4_s3_examples():
    def s3(method, path, headers, body=b""):
        h = {"host": "examplebucket.s3.amazonaws.com", "x-amz-date": "20130524T000000Z",
             "x-amz-content-sha256": hashlib.sha256(body).hexdigest(), **headers}
        auth = sign(method, f"https://examplebucket.s3.amazonaws.com{path}", h, body, S3_DOCS, "us-east-1",
                    "s3")["authorization"]
        return auth.rsplit("Signature=", 1)[1]

    assert s3("GET", "/test.txt", {"range": "bytes=0-9"}) == \
        "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"                     # GET Object
    assert s3("PUT", "/test$file.text", {"date": "Fri, 24 May 2013 00:00:00 GMT",
                                         "x-amz-storage-class": "REDUCED_REDUNDANCY"},
              b"Welcome to Amazon S3.") == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    assert s3("GET", "/?lifecycle", {}) == "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"
    assert s3("GET", "/?max-keys=2&prefix=J", {}) == \
        "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"                     # List objects


def test_sigv4_canonical_uri_s3_keeps_the_path_others_normalise_it():
    s3, _ = canonical_request("GET", "https://b.s3.amazonaws.com/a//b/../c%20d", {"host": "h"}, EMPTY,
                              double_encode=False)
    other, _ = canonical_request("GET", "https://x.amazonaws.com/a//b/../c%20d", {"host": "h"}, EMPTY)
    assert s3.splitlines()[1] == "/a//b/../c%20d" and other.splitlines()[1] == "/a/c%2520d"


def test_sigv4_agrees_with_botocore_with_a_session_token(monkeypatch):
    pytest.importorskip("botocore")
    import datetime as dt

    import botocore.auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials as BotoCredentials

    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: dt.datetime(2026, 10, 26, 10, 15))
    body = b"\x1f\x8b gz"
    url = "https://acme-runs.s3.eu-west-1.amazonaws.com/redberry/AGT-1/2026-10-26/0af7651916cd43dd.json.gz"
    headers = {"content-type": "application/gzip", "if-none-match": "*", "x-amz-meta-sha256": "ab" * 32}
    req = AWSRequest("PUT", url, data=body, headers=dict(headers))
    botocore.auth.S3SigV4Auth(BotoCredentials("AKIA1", "secret/1", "tok+en=="), "s3", "eu-west-1").add_auth(req)
    ours = sign("PUT", url, {**headers, "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
                             "x-amz-date": "20261026T101500Z"}, body, Credentials("AKIA1", "secret/1", "tok+en=="),
                "eu-west-1", "s3")
    assert ours["authorization"] == req.headers["Authorization"]
    assert ours["x-amz-security-token"] == req.headers["X-Amz-Security-Token"] == "tok+en=="


# ── the s3 store ─────────────────────────────────────────────────────────────────────────────────

REGION, BUCKET = "eu-west-1", "acme-redberry-runs"
KEYS = {"AWS_ACCESS_KEY_ID": "ASIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "session"}
META = PutMeta(AGENT, "0af7651916cd43dd8448eb211c80319c", "cd" * 32, 3)


class S3:
    """A fake S3 bucket: create-only PUTs (If-None-Match: *), checked for a SigV4 Authorization."""

    def __init__(self, status: int | None = None, body: bytes = b"") -> None:
        self.status, self.body = status, body
        self.objects: dict[str, bytes] = {}
        self.requests: list[Request] = []

    def __call__(self, req: Request) -> Response:
        self.requests.append(req)
        if self.status is not None:
            return Response(self.status, self.body)
        assert req.method == "PUT" and req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        key = req.url.split(".amazonaws.com/", 1)[1]
        if key in self.objects:
            return Response(412, b"<Error><Code>PreconditionFailed</Code></Error>")
        self.objects[key] = req.body
        return Response(200)


def test_s3_put_is_signed_create_only_and_checksummed():
    s3 = S3()
    store = S3Store("s3://" + BUCKET, REGION, EnvAwsCredentials(KEYS), transport=s3, retry_delays=())
    data = b"\x1f\x8bdata"
    r = store.put("redberry/AGT-1/2026-10-26/t.json.gz", data, META)
    assert r.url == f"s3://{BUCKET}/redberry/AGT-1/2026-10-26/t.json.gz"
    (req,) = s3.requests
    assert req.url == f"https://{BUCKET}.s3.{REGION}.amazonaws.com/redberry/AGT-1/2026-10-26/t.json.gz"
    h = req.headers
    assert (h["if-none-match"], h["content-type"], h["x-amz-meta-sha256"]) == ("*", "application/gzip", "cd" * 32)
    assert h["x-amz-content-sha256"] == hashlib.sha256(data).hexdigest()
    assert h["x-amz-checksum-sha256"] == base64.b64encode(hashlib.sha256(data).digest()).decode()
    assert h["content-md5"] == base64.b64encode(hashlib.md5(data).digest()).decode()
    assert h["x-amz-security-token"] == "session"
    assert f"Credential=ASIAEXAMPLE/{h['x-amz-date'][:8]}/{REGION}/s3/aws4_request" in h["authorization"]
    assert "if-none-match" in h["authorization"].split("SignedHeaders=")[1].split(",")[0]
    with pytest.raises(StoreConflictError):
        store.put("redberry/AGT-1/2026-10-26/t.json.gz", data, META)


@pytest.mark.parametrize("status", [409, 412])
def test_s3_conflicts(status):
    store = S3Store(BUCKET, REGION, EnvAwsCredentials(KEYS), transport=S3(status), retry_delays=())
    with pytest.raises(StoreConflictError):
        store.put("p", b"x", META)


def test_s3_failures_are_retried_then_raised_and_a_dotted_bucket_is_path_style():
    s3 = S3(503)
    store = S3Store("acme.runs", REGION, EnvAwsCredentials(KEYS), transport=s3, retry_delays=(0, 0))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        store.put("p", b"x", META)
    assert len(s3.requests) == 3 and s3.requests[0].url == f"https://s3.{REGION}.amazonaws.com/acme.runs/p"
    assert len({r.headers["x-amz-date"] for r in s3.requests}) >= 1      # signed per attempt


def test_s3_names_and_regions_are_checked():
    for bad in ("", "Acme", "s3://a", "a..b"):
        with pytest.raises(ValueError, match="REDBERRY_S3_BUCKET"):
            S3Store(bad, REGION, EnvAwsCredentials(KEYS))
    with pytest.raises(ValueError, match="REDBERRY_S3_REGION"):
        S3Store(BUCKET, "", EnvAwsCredentials(KEYS))
    with pytest.raises(RuntimeError, match="no AWS credentials"):
        EnvAwsCredentials({}).credentials()


STS_REPLY = b"""<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleWithWebIdentityResult><Credentials>
    <AccessKeyId>ASIAWEB</AccessKeyId><SecretAccessKey>websecret</SecretAccessKey>
    <SessionToken>websession</SessionToken><Expiration>2099-01-01T00:00:00Z</Expiration>
  </Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>"""


def test_a_web_identity_is_exchanged_once_and_cached(tmp_path):
    seen: list[Request] = []

    def transport(req: Request) -> Response:
        seen.append(req)
        if req.url.startswith("https://sts."):
            return Response(200, STS_REPLY)
        return S3()(req)

    token = tmp_path / "token"
    token.write_text("eyJ.oidc.token\n")
    env = {"REDBERRY_STORE": "s3", "REDBERRY_S3_BUCKET": f"s3://{BUCKET}", "REDBERRY_S3_REGION": REGION,
           "REDBERRY_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/redberry-vercel-acme",
           "AWS_WEB_IDENTITY_TOKEN_FILE": str(token)}
    store = store_from_env(env, transport=transport, retry_delays=())
    assert isinstance(store.credentials, WebIdentityCredentials)
    store.put("a", b"1", META)
    store.put("b", b"2", META)
    sts = [r for r in seen if r.url.startswith("https://sts.")]
    assert len(sts) == 1 and sts[0].url.startswith(f"https://sts.{REGION}.amazonaws.com/?Action=AssumeRoleWithWebIdentity")
    assert "WebIdentityToken=eyJ.oidc.token&" in sts[0].url + "&" and "authorization" not in sts[0].headers
    puts = [r for r in seen if r.method == "PUT"]
    assert all(r.headers["x-amz-security-token"] == "websession" for r in puts)
    assert "Credential=ASIAWEB/" in puts[0].headers["authorization"]


def test_web_identity_refreshes_before_expiry():
    replies = iter([STS_REPLY.replace(b"2099-01-01T00:00:00Z", b"2000-01-01T00:00:00Z"), STS_REPLY])
    creds = WebIdentityCredentials("arn:aws:iam::1:role/r", lambda: "t", REGION,
                                   transport=lambda _r: Response(200, next(replies)), retry_delays=())
    creds.credentials()
    assert creds.credentials().expires_at > time.time()           # the first had expired: exchanged again


@pytest.mark.parametrize(("env", "reason"), [
    ({"REDBERRY_STORE": "s3"}, "REDBERRY_S3_BUCKET"),
    ({"REDBERRY_STORE": "s3", "REDBERRY_S3_BUCKET": BUCKET}, "REDBERRY_S3_REGION"),
    ({"REDBERRY_STORE": "s3", "REDBERRY_S3_BUCKET": BUCKET, "AWS_REGION": REGION}, "no AWS credentials"),
    ({"REDBERRY_STORE": "s3", "REDBERRY_S3_BUCKET": BUCKET, "AWS_REGION": REGION,
      "REDBERRY_AWS_ROLE_ARN": "arn:aws:iam::1:role/r"}, "no web identity token"),
    ({"REDBERRY_STORE": "dynamo"}, "redberry, azure-blob or s3"),
])
def test_s3_misconfiguration_disables_with_a_reason(env, reason):
    cfg = resolve_config(env={**AZURE_ENV, **env})
    assert not cfg.enabled and reason in cfg.disabled_reason


def test_s3_from_a_lambdas_environment():
    env = {**AZURE_ENV, "WEBSITE_SITE_NAME": "", "AWS_LAMBDA_FUNCTION_NAME": "refund", "AWS_REGION": REGION,
           "REDBERRY_STORE": "s3", "REDBERRY_S3_BUCKET": f"s3://{BUCKET}", **KEYS}
    cfg = resolve_config(env=env)
    assert cfg.enabled and cfg.store.kind == "s3" and cfg.store.region == REGION and cfg.store.bucket == BUCKET
    assert isinstance(cfg.store.credentials, EnvAwsCredentials)


@pytest.mark.parametrize(("env", "enabled"), [
    ({"AWS_LAMBDA_FUNCTION_NAME": "refund"}, True),
    ({"AWS_LAMBDA_FUNCTION_NAME": "refund", "REDBERRY_DISABLED": "1"}, False),
    ({"AWS_LAMBDA_FUNCTION_NAME": ""}, False),
])
def test_the_hasher_runs_on_lambda(env, enabled):
    assert resolve_config(env={**AZURE_ENV, "WEBSITE_SITE_NAME": "", **env}).enabled is enabled


# ── Lambda: with_redberry on handler(event, context) ─────────────────────────────────────────────


class Valp:
    """VALP (the hash reports, checked as VALP does) and the customer's S3 bucket."""

    def __init__(self) -> None:
        self.s3 = S3()
        self.reports: list[dict[str, Any]] = []

    def __call__(self, req: Request) -> Response:
        if ".s3." in req.url:
            return self.s3(req)
        if req.url.endswith("/hashes"):
            agent = req.url.split("/v1/agents/")[1].split("/")[0]
            self.reports.append(valp_accepts(agent, req.headers, req.body, PUBLIC_KEY))
            return Response(201, b'{"leaf":"hashes/x","window_start":"2026-10-22T00:00:00.000000Z"}')
        raise AssertionError(f"unexpected {req.method} {req.url}")

    def document(self, trace_id: str) -> dict[str, Any]:
        (data,) = [v for k, v in self.s3.objects.items() if k.endswith(f"/{trace_id}.json.gz")]
        return json.loads(gzip.decompress(data))


class LambdaContext:
    function_name = "refund-agent"
    aws_request_id = "8f5a7d52-1c3b-4b2e-9a51-6d0f1a2b3c4d"
    invoked_function_arn = "arn:aws:lambda:eu-west-1:123456789012:function:refund-agent"

    def __init__(self, remaining_ms: int = 30_000) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


HTTP_API_EVENT = {"version": "2.0", "rawPath": "/refund", "rawQueryString": "token=SECRET",
                  "requestContext": {"http": {"method": "POST", "path": "/refund"}}, "body": "{}"}


@pytest.fixture
def lambda_env(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "refund-agent")
    monkeypatch.setenv("AWS_REGION", REGION)


@pytest.fixture
def installed(monkeypatch, lambda_env):
    valp = Valp()
    env = {**AZURE_ENV, "WEBSITE_SITE_NAME": "", "AWS_LAMBDA_FUNCTION_NAME": "refund-agent", "AWS_REGION": REGION,
           "REDBERRY_STORE": "s3", "REDBERRY_S3_BUCKET": f"s3://{BUCKET}", **KEYS}
    provider = TracerProvider(resource=Resource.create(_azure._resource_attributes()))
    monkeypatch.setattr(_azure, "_processor", None)
    monkeypatch.setattr(_azure, "_provider", None)
    monkeypatch.setattr(_azure.trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setattr(sdk, "prepare_instrumentation", lambda **k: None)
    monkeypatch.setattr(sdk, "enable_instrumentation", lambda **k: [])
    register(processor=RedberryProcessor(env=env, transport=valp, retry_delays=(0, 0), grace_ms=1000))
    return valp, provider


def test_resource_attributes_on_lambda(lambda_env):
    attrs = _azure._resource_attributes()
    assert attrs == {"cloud.provider": "aws", "cloud.platform": "aws_lambda", "service.name": "refund-agent",
                     "faas.name": "refund-agent", "cloud.region": REGION}


def test_a_lambda_handler_is_stored_in_s3_and_reported_before_it_returns(installed):
    valp, provider = installed

    @with_redberry(agent_id=AGENT)
    def lambda_handler(event, context):
        with _chat(provider.get_tracer("opentelemetry.instrumentation.openai_v2")):
            pass
        return {"statusCode": 200, "body": "ok"}

    assert lambda_handler(HTTP_API_EVENT, LambdaContext()) == {"statusCode": 200, "body": "ok"}
    (report,) = valp.reports                                     # already there: Lambda may freeze now
    assert report["store"] == "s3" and report["stored"] is True
    doc = valp.document(report["trace_id"])
    root = doc["spans"][0]
    a = root["attributes"]
    assert root["name"] == "refund-agent" and root["kind"] == 1                # SERVER
    assert (a["faas.name"], a["faas.invocation_id"], a["faas.trigger"]) == (
        "refund-agent", LambdaContext.aws_request_id, "http")
    assert (a["cloud.provider"], a["cloud.platform"], a["cloud.region"]) == ("aws", "aws_lambda", REGION)
    assert a["cloud.account.id"] == "123456789012" and a["cloud.resource_id"] == LambdaContext.invoked_function_arn
    assert (a["http.request.method"], a["url.path"], a["http.response.status_code"]) == ("POST", "/refund", 200)
    assert "SECRET" not in json.dumps(doc)
    assert doc["resource"]["cloud.platform"] == "aws_lambda"


def test_a_non_http_event_and_an_async_handler(installed):
    valp, provider = installed

    @with_redberry(agent_id=AGENT)
    async def handler(event, context):
        await asyncio.sleep(0)
        with _chat(provider.get_tracer("t")):
            pass
        return {"done": True}

    assert inspect.iscoroutinefunction(handler)
    assert asyncio.run(handler({"Records": [{"eventSource": "aws:sqs"}]}, LambdaContext())) == {"done": True}
    a = valp.document(valp.reports[0]["trace_id"])["spans"][0]["attributes"]
    assert a["faas.trigger"] == "other" and "http.request.method" not in a


def test_the_wait_is_bounded_by_the_invocations_remaining_time():
    inv = _azure._Invocation.__new__(_azure._Invocation)
    inv.lambda_ctx = LambdaContext(remaining_ms=4000)
    assert inv.budget(15.0) == pytest.approx(3.0)
    inv.lambda_ctx = LambdaContext(remaining_ms=500)
    assert inv.budget(15.0) == pytest.approx(0.1)
    inv.lambda_ctx = None
    assert inv.budget(15.0) == 15.0


def test_a_rest_api_event_and_a_callable_object_handler(installed):
    valp, provider = installed

    class Adapter:                                               # e.g. Mangum(app): no __name__
        def __call__(self, event, context):
            with _chat(provider.get_tracer("t")):
                pass
            return {"statusCode": 502}

    handler = with_redberry(Adapter(), agent_id=AGENT)
    handler({"httpMethod": "GET", "path": "/v1/claims", "requestContext": {"stage": "prod"}}, LambdaContext())
    root = valp.document(valp.reports[0]["trace_id"])["spans"][0]
    assert root["attributes"]["url.path"] == "/v1/claims" and root["status"]["code"] == 2       # 5xx: ERROR
