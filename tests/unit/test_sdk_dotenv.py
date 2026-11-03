"""redberry_sdk.init() picks up OTELLINE_AGENT_ID from the .env `redberry connect` writes, and nothing else."""

from __future__ import annotations

import os

from redberry_sdk import _sdk


def test_only_otelline_keys_come_from_the_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OTELLINE_AGENT_ID=AGT-01M4XVSTBFVZZWAZ1D0FCT0Y8A\nOPENAI_API_KEY=sk-not-for-us\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OTELLINE_AGENT_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _sdk._load_otelline_dotenv()
    assert os.environ["OTELLINE_AGENT_ID"] == "AGT-01M4XVSTBFVZZWAZ1D0FCT0Y8A"
    assert "OPENAI_API_KEY" not in os.environ
    monkeypatch.delenv("OTELLINE_AGENT_ID")


def test_an_id_already_set_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OTELLINE_AGENT_ID=AGT-FROM-FILE\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OTELLINE_AGENT_ID", "AGT-FROM-ENV")
    _sdk._load_otelline_dotenv()
    assert os.environ["OTELLINE_AGENT_ID"] == "AGT-FROM-ENV"
