"""Unit tests for SDKConfig and OTELLINE_* env-var overrides."""
import pytest

from redberry_sdk._config import SDKConfig


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (
        "OTELLINE_AGENT_ID",
        "OTELLINE_RUN_ID",
        "OTELLINE_TASK_CLASS",
        "OTELLINE_OTEL_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


class TestApplyEnvOverrides:
    def test_run_id_filled_from_env(self, monkeypatch):
        monkeypatch.setenv("OTELLINE_RUN_ID", "rap-run-42")
        cfg = SDKConfig(agent_id="a").apply_env_overrides()
        assert cfg.run_id == "rap-run-42"

    def test_explicit_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("OTELLINE_RUN_ID", "env-id")
        cfg = SDKConfig(agent_id="a", run_id="explicit-id").apply_env_overrides()
        assert cfg.run_id == "explicit-id"

    def test_task_class_filled_from_env(self, monkeypatch):
        monkeypatch.setenv("OTELLINE_TASK_CLASS", "rag_qa")
        cfg = SDKConfig(agent_id="a").apply_env_overrides()
        assert cfg.task_class == "rag_qa"

    def test_otel_endpoint_overridden_when_default(self, monkeypatch):
        monkeypatch.setenv("OTELLINE_OTEL_ENDPOINT", "http://collector:4318")
        cfg = SDKConfig(agent_id="a").apply_env_overrides()
        assert cfg.otel_endpoint == "http://collector:4318"

    def test_explicit_otel_endpoint_wins(self, monkeypatch):
        monkeypatch.setenv("OTELLINE_OTEL_ENDPOINT", "http://collector:4318")
        cfg = SDKConfig(
            agent_id="a", otel_endpoint="http://override:9999"
        ).apply_env_overrides()
        assert cfg.otel_endpoint == "http://override:9999"

    def test_no_env_no_overrides(self):
        cfg = SDKConfig(agent_id="a").apply_env_overrides()
        assert cfg.run_id is None
        assert cfg.task_class is None
        assert cfg.otel_endpoint == "http://localhost:4318"

    def test_empty_env_string_treated_as_unset(self, monkeypatch):
        """RAP outer loop may pass empty strings; treat them as 'not set'."""
        monkeypatch.setenv("OTELLINE_RUN_ID", "")
        cfg = SDKConfig(agent_id="a").apply_env_overrides()
        assert cfg.run_id is None

    def test_whitespace_only_env_string_treated_as_unset(self, monkeypatch):
        """Whitespace-only env values must not produce malformed identifiers."""
        monkeypatch.setenv("OTELLINE_RUN_ID", "   ")
        monkeypatch.setenv("OTELLINE_TASK_CLASS", " ")
        cfg = SDKConfig(agent_id="a").apply_env_overrides()
        assert cfg.run_id is None
        assert cfg.task_class is None

    def test_env_values_are_stripped(self, monkeypatch):
        """Surrounding whitespace is dropped — env shells often inject it."""
        monkeypatch.setenv("OTELLINE_RUN_ID", "  rap-run-1  ")
        cfg = SDKConfig(agent_id="a").apply_env_overrides()
        assert cfg.run_id == "rap-run-1"
