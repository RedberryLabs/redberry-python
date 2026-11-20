"""Each SDK-side install failure carries a stable code (RB-SDK-<AREA>-<NNN>) at the start of its message."""

import logging
from types import SimpleNamespace

import pytest
import requests

import redberry_sdk
from redberry_sdk import _emit, _emit_aws, _emit_azure, _sdk


@pytest.fixture(autouse=True)
def clean():
    _emit._reset_for_tests()
    yield
    _emit._reset_for_tests()


def test_missing_instrumentation_has_its_code(monkeypatch):
    gap = SimpleNamespace(library="openai", detail="not importable", extra="openai")
    monkeypatch.setattr(_sdk, "missing_instrumentation", lambda: [gap])
    with pytest.raises(redberry_sdk.MissingInstrumentationError, match=r"^RB-SDK-INIT-001: ") as e:
        _sdk.prepare_instrumentation()
    assert e.value.code == redberry_sdk.MissingInstrumentationError.code == "RB-SDK-INIT-001"


def test_instrumentation_that_fails_to_start_has_its_code(monkeypatch):
    gap = SimpleNamespace(library="openai", detail="raised on instrument()")
    monkeypatch.setattr(_sdk, "set_logger_provider", lambda provider: None)
    monkeypatch.setattr(_sdk, "activate_all", lambda **kw: [])
    monkeypatch.setattr(_sdk, "failed_to_enable", lambda statuses: [gap])
    with pytest.raises(redberry_sdk.MissingInstrumentationError, match=r"^RB-SDK-INIT-001: "):
        _sdk.enable_instrumentation()


def test_content_capture_off_has_its_code(monkeypatch):
    monkeypatch.delenv(_sdk._CAPTURE_ENV, raising=False)
    with pytest.raises(redberry_sdk.ContentCaptureDisabledError, match=r"^RB-SDK-INIT-002: ") as e:
        _sdk._check_content_capture(SimpleNamespace(capture_content=False, allow_content_capture_off=False))
    assert e.value.code == redberry_sdk.ContentCaptureDisabledError.code == "RB-SDK-INIT-002"


def _codes(caplog, prefix):
    return [r for r in caplog.records if r.message.startswith(f"redberry_sdk: {prefix}: ")]


def test_gcp_without_an_id_token_has_its_code(monkeypatch, caplog):
    monkeypatch.setattr(_emit, "_metadata_get", lambda path: None)
    with caplog.at_level(logging.WARNING, logger="redberry.sdk"), pytest.raises(_emit.TokenUnavailable):
        _emit.IdTokenAuth("https://a").token()
    [record] = _codes(caplog, "RB-SDK-TOKEN-001")
    assert record.levelno == logging.WARNING and "on GCP" in record.message


def test_aws_without_credentials_has_its_code(caplog):
    auth = _emit_aws.SigV4Auth(_emit_aws.AwsCredentials({}), "eu-west-1")
    with caplog.at_level(logging.WARNING, logger="redberry.sdk"), pytest.raises(_emit.TokenUnavailable):
        auth(requests.Request("POST", "https://x.lambda-url.eu-west-1.on.aws/", data=b"x").prepare())
    [record] = _codes(caplog, "RB-SDK-TOKEN-001")
    assert record.levelno == logging.WARNING and "on AWS" in record.message


def test_azure_without_a_managed_identity_token_has_its_code(caplog):
    def down(*a):
        raise OSError("unreachable")
    env = {"IDENTITY_ENDPOINT": "http://localhost:1/msi", "IDENTITY_HEADER": "h"}
    auth = _emit_azure.BearerAuth(_emit_azure.ManagedIdentityToken("api://app", env, down))
    with caplog.at_level(logging.WARNING, logger="redberry.sdk"), pytest.raises(_emit.TokenUnavailable):
        auth(requests.Request("POST", "https://ingest.example", data=b"x").prepare())
    [record] = _codes(caplog, "RB-SDK-TOKEN-001")
    assert record.levelno == logging.WARNING and "on Azure" in record.message


def test_dropped_spans_have_their_code(caplog):
    with caplog.at_level(logging.WARNING, logger="redberry.sdk"):
        _emit._count_dropped(2)
    [record] = _codes(caplog, "RB-SDK-DROP-001")
    assert record.levelno == logging.WARNING and "2 span(s) dropped so far" in record.message


@pytest.mark.parametrize("auth", [None, object.__new__(_emit_aws.SigV4Auth)])
def test_ingest_refusal_has_its_code(monkeypatch, caplog, auth):
    monkeypatch.setattr(_emit, "_service_account", lambda: "agent@p.iam.gserviceaccount.com")
    retry = object.__new__(_emit.AuthRetryExporter)
    retry._auth = auth
    with caplog.at_level(logging.WARNING, logger="redberry.sdk"):
        retry._refused(403)
    [record] = _codes(caplog, "RB-SDK-AUTH-001")
    assert record.levelno == logging.ERROR and "(403)" in record.message
