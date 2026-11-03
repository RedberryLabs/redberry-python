"""A3 of the one-chain plan: content capture cannot be switched off silently, and every span says
which code and manifest produced it.

Before, ``init()`` did ``os.environ.setdefault(..._CAPTURE_MESSAGE_CONTENT, "true")``, so a ``false``
inherited from the environment won without a word and the agent ran metadata-only. Now an off
switch from either place stops ``init()``, unless the run is metadata-only on purpose
(``allow_content_capture_off``).
"""
from __future__ import annotations

import hashlib

import pytest

import redberry_sdk
from redberry_sdk import _sdk
from redberry_sdk._config import SDKConfig

ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (ENV, "OTELLINE_ALLOW_CONTENT_CAPTURE_OFF", "OTELLINE_AGENT_CODE_VERSION", "OTELLINE_MANIFEST"):
        monkeypatch.delenv(name, raising=False)


def test_capture_on_by_default_passes() -> None:
    _sdk._check_content_capture(SDKConfig(agent_id="a"))


def test_an_inherited_false_no_longer_wins_silently(monkeypatch) -> None:
    monkeypatch.setenv(ENV, "false")
    with pytest.raises(redberry_sdk.ContentCaptureDisabledError) as e:
        _sdk._check_content_capture(SDKConfig(agent_id="a"))
    assert ENV in str(e.value) and "REDBERRY_ALLOW_CONTENT_CAPTURE_OFF" in str(e.value)


@pytest.mark.parametrize("value", ["False", "0", "no", ""])
def test_anything_but_true_is_off(monkeypatch, value) -> None:
    monkeypatch.setenv(ENV, value)
    with pytest.raises(redberry_sdk.ContentCaptureDisabledError):
        _sdk._check_content_capture(SDKConfig(agent_id="a"))


def test_capture_content_false_is_refused() -> None:
    with pytest.raises(redberry_sdk.ContentCaptureDisabledError, match="capture_content=False"):
        _sdk._check_content_capture(SDKConfig(agent_id="a", capture_content=False))


def test_metadata_only_on_purpose_runs_and_says_so(monkeypatch) -> None:
    with pytest.warns(RuntimeWarning, match="WITHOUT content capture"):
        _sdk._check_content_capture(SDKConfig(agent_id="a", capture_content=False, allow_content_capture_off=True))
    monkeypatch.setenv("OTELLINE_ALLOW_CONTENT_CAPTURE_OFF", "1")
    assert SDKConfig(agent_id="a").apply_env_overrides().allow_content_capture_off is True


def test_init_refuses_before_setting_anything_up(monkeypatch) -> None:
    monkeypatch.setenv(ENV, "false")
    monkeypatch.setattr(_sdk, "_INITIALIZED", False)
    monkeypatch.setattr(_sdk, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(_sdk, "missing_instrumentation", lambda: [])
    with pytest.raises(redberry_sdk.ContentCaptureDisabledError):
        redberry_sdk.init(agent_id="t")
    assert _sdk._TRACER_PROVIDER is None and _sdk._INITIALIZED is False


def test_the_fingerprint_names_the_code_and_the_manifest(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b'{"schema_version": "0.2", "agent_id": "a"}\n')
    attrs = SDKConfig(agent_id="a", agent_code_version="git:4235ff4", manifest_path=str(manifest)).fingerprint_attributes()
    # The manifest hash is VALP genesis's: the SHA-256 of the file's bytes exactly.
    assert attrs == {"redberry.agent_code_version": "git:4235ff4",
                     "redberry.manifest_hash": hashlib.sha256(manifest.read_bytes()).hexdigest()}


def test_the_manifest_defaults_to_where_the_extractor_writes_it(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert SDKConfig(agent_id="a").fingerprint_attributes() == {}   # none yet: nothing reported
    (tmp_path / ".redberry").mkdir()
    (tmp_path / ".redberry" / "manifest.json").write_bytes(b"{}")
    assert SDKConfig(agent_id="a").fingerprint_attributes()["redberry.manifest_hash"] == hashlib.sha256(b"{}").hexdigest()


def test_the_fingerprint_comes_from_the_environment_too(tmp_path, monkeypatch) -> None:
    (tmp_path / "m.json").write_bytes(b"[]")
    monkeypatch.setenv("OTELLINE_AGENT_CODE_VERSION", "sha256:abc")
    monkeypatch.setenv("OTELLINE_MANIFEST", str(tmp_path / "m.json"))
    attrs = SDKConfig(agent_id="a").apply_env_overrides().fingerprint_attributes()
    assert attrs["redberry.agent_code_version"] == "sha256:abc" and attrs["redberry.manifest_hash"] == hashlib.sha256(b"[]").hexdigest()


def test_an_unreadable_manifest_is_logged_not_fatal(tmp_path, caplog) -> None:
    attrs = SDKConfig(agent_id="a", manifest_path=str(tmp_path / "missing.json")).fingerprint_attributes()
    assert attrs == {} and "cannot read the manifest" in caplog.text
