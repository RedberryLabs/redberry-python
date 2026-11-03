"""The emit path on AWS (redberry_sdk._emit_aws): discovery via STS and the installation's bucket,
container credentials, SigV4 on every export, and the overrides. No AWS call: a fake transport answers."""
from __future__ import annotations

import hashlib
import json
import re

import pytest
import requests

from redberry_sdk import _emit, _emit_aws
from redberry_sdk.hasher import _sigv4

ACCT = "123456789012"
REGION = "eu-west-1"
FN_URL = "https://abc123.lambda-url.eu-west-1.on.aws"
STS_XML = (f"<GetCallerIdentityResponse><GetCallerIdentityResult><Arn>arn:aws:sts::{ACCT}:assumed-role/r/f</Arn>"
           f"<UserId>AROA:f</UserId><Account>{ACCT}</Account></GetCallerIdentityResult></GetCallerIdentityResponse>")


class FakeAws:
    def __init__(self, bucket_owner=ACCT, creds_reply=None):
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.bucket_owner = bucket_owner
        self.creds_reply = creds_reply

    def __call__(self, method, url, headers, body):
        h = {k.lower(): v for k, v in headers.items()}
        self.calls.append((method, url, h, body))
        if url.startswith("https://sts."):
            return 200, {}, STS_XML.encode()
        if ".s3." in url:
            if h.get("x-amz-expected-bucket-owner") != self.bucket_owner:
                return 403, {}, b"<Error><Code>AccessDenied</Code></Error>"
            return 200, {}, json.dumps({"url": FN_URL + "/", "region": REGION}).encode()
        if self.creds_reply is not None:
            return 200, {}, json.dumps(self.creds_reply).encode()
        return 404, {}, b""


LAMBDA_ENV = {"AWS_LAMBDA_FUNCTION_NAME": "refund-agent", "AWS_REGION": REGION,
              "AWS_ACCESS_KEY_ID": "ASIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "tok"}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in (_emit.ENDPOINT_ENV, _emit.INGEST_URL_ENV, "REDBERRY_CLOUD", "AWS_LAMBDA_FUNCTION_NAME",
                 "ECS_CONTAINER_METADATA_URI_V4", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID",
                 "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                 "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    _emit._reset_for_tests()
    yield
    _emit._reset_for_tests()


def _auth_parts(value: str) -> dict:
    m = re.fullmatch(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request, "
                     r"SignedHeaders=([^,]+), Signature=([0-9a-f]{64})", value)
    assert m, value
    return dict(zip(("key", "date", "region", "service", "signed", "sig"), m.groups(), strict=True))


def test_on_aws_detection():
    assert _emit_aws.on_aws({"AWS_LAMBDA_FUNCTION_NAME": "f"})
    assert _emit_aws.on_aws({"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/x"})
    assert _emit_aws.on_aws({"REDBERRY_CLOUD": "AWS"})
    assert not _emit_aws.on_aws({"K_SERVICE": "x"})


def test_discovery_chain_signed_and_parsed():
    fake = FakeAws()
    url, region, _ = _emit_aws.discover(LAMBDA_ENV, fake)
    assert (url, region) == (FN_URL, REGION)
    (m1, sts_url, sts_h, sts_body), (m2, s3_url, s3_h, _) = fake.calls
    assert (m1, sts_url) == ("POST", f"https://sts.{REGION}.amazonaws.com/")
    assert b"Action=GetCallerIdentity" in sts_body and b"Version=2011-06-15" in sts_body
    a = _auth_parts(sts_h["authorization"])
    assert (a["key"], a["region"], a["service"]) == ("ASIAEXAMPLE", REGION, "sts")
    assert sts_h["x-amz-security-token"] == "tok"
    assert (m2, s3_url) == ("GET", f"https://redberry-{ACCT}-{REGION}.s3.{REGION}.amazonaws.com/ingest.json")
    assert s3_h["x-amz-expected-bucket-owner"] == ACCT
    a = _auth_parts(s3_h["authorization"])
    assert a["service"] == "s3" and "x-amz-expected-bucket-owner" in a["signed"].split(";")
    assert s3_h["x-amz-content-sha256"] == _sigv4.EMPTY_SHA256


def test_bucket_owned_by_another_account_is_refused():
    with pytest.raises(_emit_aws.AwsDiscoveryError, match="403"):
        _emit_aws.discover(LAMBDA_ENV, FakeAws(bucket_owner="999999999999"))


def test_no_region_is_an_error():
    env = dict(LAMBDA_ENV)
    del env["AWS_REGION"]
    with pytest.raises(_emit_aws.AwsDiscoveryError):
        _emit_aws.discover(env, FakeAws())
    env["AWS_DEFAULT_REGION"] = REGION
    assert _emit_aws.discover(env, FakeAws())[1] == REGION


def test_ecs_container_credentials_and_refresh():
    clock = [1_000_000.0]
    reply = {"AccessKeyId": "ASIAECS", "SecretAccessKey": "s", "Token": "ecs-tok",
             "Expiration": "1970-01-12T14:00:00Z"}  # 1_000_800: refresh at 1_000_500
    fake = FakeAws(creds_reply=reply)
    env = {"ECS_CONTAINER_METADATA_URI_V4": "x", "AWS_REGION": REGION,
           "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"}
    creds = _emit_aws.AwsCredentials(env, fake, clock=lambda: clock[0])
    c = creds.credentials()
    assert (c.access_key_id, c.session_token) == ("ASIAECS", "ecs-tok")
    assert fake.calls[0][1] == "http://169.254.170.2/v2/credentials/abc"
    creds.credentials()
    assert len(fake.calls) == 1  # cached
    clock[0] = 1_000_600.0
    creds.credentials()
    assert len(fake.calls) == 2  # refreshed before expiry


def test_ecs_full_uri_with_auth_token():
    fake = FakeAws(creds_reply={"AccessKeyId": "A", "SecretAccessKey": "s", "Token": "t"})
    env = {"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:9000/creds",
           "AWS_CONTAINER_AUTHORIZATION_TOKEN": "Bearer xyz"}
    _emit_aws.AwsCredentials(env, fake).credentials()
    _, url, headers, _ = fake.calls[0]
    assert url == "http://127.0.0.1:9000/creds" and headers["authorization"] == "Bearer xyz"


def test_resolve_endpoint_discovers_on_aws_and_caches(monkeypatch):
    for k, v in LAMBDA_ENV.items():
        monkeypatch.setenv(k, v)
    fake = FakeAws()
    monkeypatch.setattr(_emit_aws, "urllib_transport", fake)
    monkeypatch.setattr(_emit, "discover_gcp", lambda: pytest.fail("GCP probed on AWS"))
    ep = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    assert (ep.url, ep.source, ep.aws_region) == (FN_URL, "aws", REGION)
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True) is ep
    assert len(fake.calls) == 2
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=False).source == "default"


def test_discovery_failure_falls_back(monkeypatch, caplog):
    for k, v in LAMBDA_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(_emit_aws, "urllib_transport", FakeAws(bucket_owner="0"))
    monkeypatch.setattr(_emit, "discover_gcp", lambda: None)
    ep = _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True)
    assert ep.source == "default"
    assert "could not be discovered" in caplog.text


@pytest.mark.parametrize("env", [_emit.INGEST_URL_ENV, _emit.ENDPOINT_ENV])
def test_overrides_skip_discovery(monkeypatch, env):
    for k, v in LAMBDA_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(env, "https://ingest.example/")
    monkeypatch.setattr(_emit_aws, "urllib_transport", lambda *a: pytest.fail("discovery ran"))
    configured = "https://ingest.example" if env == _emit.ENDPOINT_ENV else _emit.DEFAULT_ENDPOINT
    ep = _emit.resolve_endpoint(configured, discover=True)
    assert (ep.url, ep.source) == ("https://ingest.example", "explicit")
    assert _emit.build_session(ep) is None


def test_gcp_unchanged_off_aws(monkeypatch):
    gcp = _emit.Endpoint(url="https://redberry-ingest-1.r.run.app", source="gcp", audience="a")
    monkeypatch.setattr(_emit, "discover_gcp", lambda: gcp)
    monkeypatch.setattr(_emit_aws, "urllib_transport", lambda *a: pytest.fail("AWS probed on GCP"))
    assert _emit.resolve_endpoint(_emit.DEFAULT_ENDPOINT, discover=True) is gcp
    assert isinstance(_emit.build_session(gcp).auth, _emit.IdTokenAuth)


def test_export_is_sigv4_signed_for_lambda():
    creds = _emit_aws.AwsCredentials(LAMBDA_ENV)
    ep = _emit.Endpoint(url=FN_URL, source="aws", aws_region=REGION, aws_credentials=creds)
    session = _emit.build_session(ep)
    assert isinstance(session.auth, _emit_aws.SigV4Auth)
    body = b"\x0a\x01otlp-protobuf"
    prepared = session.prepare_request(requests.Request(
        "POST", FN_URL + "/v1/traces", data=body, headers={"Content-Type": "application/x-protobuf"}))
    h = prepared.headers
    a = _auth_parts(h["Authorization"])
    assert (a["key"], a["region"], a["service"]) == ("ASIAEXAMPLE", REGION, "lambda")
    assert h["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
    assert h["x-amz-security-token"] == "tok"
    assert {"host", "x-amz-date", "x-amz-content-sha256", "x-amz-security-token", "content-type"} <= set(
        a["signed"].split(";"))
    # The signature verifies: re-sign with the same date and compare.
    again = _sigv4.sign("POST", FN_URL + "/v1/traces",
                        {"content-type": "application/x-protobuf", "x-amz-date": h["x-amz-date"],
                         "x-amz-content-sha256": h["x-amz-content-sha256"]},
                        body, creds.credentials(), REGION, "lambda", payload_sha256=h["x-amz-content-sha256"])
    assert again["authorization"] == h["Authorization"]


def test_export_without_credentials_is_transient():
    creds = _emit_aws.AwsCredentials({})
    auth = _emit_aws.SigV4Auth(creds, REGION)
    with pytest.raises(_emit.TokenUnavailable):
        auth(requests.Request("POST", FN_URL, data=b"x").prepare())


def test_processor_keeps_auth_retry_for_aws():
    ep = _emit.Endpoint(url=FN_URL, source="aws", aws_region=REGION,
                        aws_credentials=_emit_aws.AwsCredentials(LAMBDA_ENV))
    processor = _emit.build_processor(ep)
    try:
        assert isinstance(processor._auth_retry._auth, _emit_aws.SigV4Auth)
    finally:
        processor.shutdown()


def test_coverage_report_never_fails_init_on_a_read_only_working_directory(tmp_path, monkeypatch):
    # AWS Lambda's working directory (/var/task) is read-only: the report goes to the temp directory.
    from redberry_sdk._coverage import CoverageReport

    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    tmp = tmp_path / "tmp"
    monkeypatch.chdir(ro)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp))
    report = CoverageReport(agent_id="a", generated_at="2026-10-29T00:00:00Z", instrumentors=[])
    out = report.persist()
    assert out == tmp / ".redberry" / "coverage.json" and out.exists()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(ro))
    assert report.persist() is None  # nowhere writable: logged, not raised


def test_flush_after_sends_the_spans_before_the_handler_returns(monkeypatch):
    # A frozen-between-requests platform (Lambda): the handler's spans are flushed on return and on error.
    import redberry_sdk
    from redberry_sdk import _sdk

    calls = []

    class Provider:
        def force_flush(self, timeout_millis):
            calls.append(timeout_millis)
            return True

    monkeypatch.setattr(_sdk, "_TRACER_PROVIDER", Provider())

    @redberry_sdk.flush_after
    def handler(event, context):
        if event == "boom":
            raise RuntimeError("boom")
        return "ok"

    assert handler("e", None) == "ok" and calls == [30000]
    try:
        handler("boom", None)
    except RuntimeError:
        pass
    assert calls == [30000, 30000]
    monkeypatch.setattr(_sdk, "_TRACER_PROVIDER", None)
    assert redberry_sdk.flush() is True  # before init: a no-op


def test_lambda_resource_attributes_name_the_function(monkeypatch):
    # The scoring job stages the agent's code from faas.name + cloud.region (lambda:GetFunction).
    from redberry_sdk._emit import cloud_run_resource_attributes

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "refund-agent")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_VERSION", "$LATEST")
    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    assert cloud_run_resource_attributes() == {
        "cloud.provider": "aws", "cloud.platform": "aws_lambda", "faas.name": "refund-agent",
        "faas.version": "$LATEST", "cloud.region": "eu-west-2",
    }
