"""Settings are read as REDBERRY_*; the earlier OTELLINE_* spelling still works."""

import pytest

from redberry_sdk import _env
from redberry_sdk._config import SDKConfig
from redberry_sdk._emit import auth_retry_budget


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for prefix in ("REDBERRY_", "OTELLINE_"):
        for name in ("AGENT_ID", "RUN_ID", "OTEL_ENDPOINT", "AUTH_RETRY_S", "ALLOW_MISSING_INSTRUMENTATION"):
            monkeypatch.delenv(prefix + name, raising=False)


def test_the_new_name_is_read(monkeypatch):
    monkeypatch.setenv("REDBERRY_RUN_ID", "r-new")
    assert _env.get("REDBERRY_RUN_ID") == "r-new"


def test_the_earlier_name_still_works(monkeypatch):
    monkeypatch.setenv("OTELLINE_RUN_ID", "r-old")
    assert _env.get("REDBERRY_RUN_ID") == "r-old"


def test_the_new_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("OTELLINE_RUN_ID", "r-old")
    monkeypatch.setenv("REDBERRY_RUN_ID", "r-new")
    assert _env.get("REDBERRY_RUN_ID") == "r-new"


def test_names_outside_the_prefix_have_no_fallback(monkeypatch):
    monkeypatch.setenv("OTELLINE_VALP_X", "1")
    assert _env.get("VALP_X") is None


@pytest.mark.parametrize("prefix", ["REDBERRY_", "OTELLINE_"])
def test_config_reads_either_spelling(monkeypatch, prefix):
    monkeypatch.setenv(prefix + "RUN_ID", "run-1")
    monkeypatch.setenv(prefix + "ALLOW_MISSING_INSTRUMENTATION", "1")
    cfg = SDKConfig(agent_id="a").apply_env_overrides()
    assert cfg.run_id == "run-1" and cfg.allow_missing_instrumentation is True


@pytest.mark.parametrize("prefix", ["REDBERRY_", "OTELLINE_"])
def test_the_auth_retry_window_reads_either_spelling(monkeypatch, prefix):
    monkeypatch.setenv(prefix + "AUTH_RETRY_S", "42")
    assert auth_retry_budget() == 42
