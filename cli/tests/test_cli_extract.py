"""`connect --extract` when redberry-extract is not on PATH: the extractor licence, the download token, and
the install with uv into ``$REDBERRY_HOME/extractor/<version>``, against a fake Redberry API over real HTTP.

The download token must reach uv only in its environment: never in argv, a file or an error message.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from redberry_cli import main as cli

TOKEN = "ya29.secret-download-token"
INDEX = "https://europe-python.pkg.dev/redberry/extractor/simple/"
ENV = "UV_INDEX_REDBERRY_EXTRACTOR"


class _Api(BaseHTTPRequestHandler):
    state: ClassVar[dict] = {}

    def log_message(self, *args):  # quiet
        pass

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        return self.headers.get("Authorization") == "Bearer rbcli_session"

    def do_GET(self):
        s = self.state
        if self.path == "/api/cli/extractor-licence" and self._authed():
            return self._reply(200, {"version": "2026-11", "text": "THE LICENCE TEXT", "accepted": s["accepted"]})
        self._reply(401, {"detail": "Run `redberry login` first"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        s = self.state
        if not self._authed():
            return self._reply(401, {"detail": "Run `redberry login` first"})
        if self.path == "/api/cli/extractor-licence":
            s["accept_posts"] = s.get("accept_posts", 0) + 1
            if body.get("version") != "2026-11":
                return self._reply(409, {"detail": {"error": "licence_version_mismatch", "message": "old",
                                                    "version": "2026-11"}})
            s["accepted"] = True
            return self._reply(200, {"version": "2026-11", "accepted_at": "2026-11-17T12:00:00Z"})
        if self.path == "/api/cli/extractor-token":
            s["token_posts"] = s.get("token_posts", 0) + 1
            if s.get("token_status"):
                return self._reply(s["token_status"], {"detail": {"error": "x", "message": "nope", "version": "1"}})
            if not s["accepted"]:
                return self._reply(403, {"detail": {"error": "licence_not_accepted", "message": "accept it",
                                                    "version": "2026-11"}})
            return self._reply(200, {"index_url": INDEX, "username": "oauth2accesstoken", "access_token": TOKEN,
                                     "expires_at": int(time.time()) + 3600, "version_pin": "0.4.1"})
        self._reply(404, {"detail": "Not Found"})


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("REDBERRY_HOME", str(tmp_path / "rbhome"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _Api.state = {"accepted": False}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "agent"
    repo.mkdir()
    (repo / "agent.py").write_text("import anthropic\nclient = anthropic.Anthropic()\n")
    return repo


def _sign_in(api: str) -> None:
    creds = {api: {"token": "rbcli_session", "company": {"id": "co_acme", "name": "Acme Ltd"},
                   "expires_at": time.time() + 3600}}
    cli._save_credentials(creds)


class _Run:
    """A fake subprocess.run: uv creates the venv and installs the command; the extractor writes the manifest."""

    def __init__(self, fail_install: bool = False):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.fail_install = fail_install

    def __call__(self, cmd, cwd=None, env=None, **kw):
        self.calls.append((list(cmd), env))
        rc = 0
        if cmd[0] == "/fake/uv" and cmd[1] == "venv":
            Path(cmd[2]).mkdir(parents=True)
        elif cmd[0] == "/fake/uv" and "redberry-extractor[llm]==0.4.1" in cmd:
            if self.fail_install:
                return type("R", (), {"returncode": 1, "stderr": f"401 for https://x:{TOKEN}@host", "stdout": ""})()
            exe = cli._extractor_exe(Path(cmd[cmd.index("--python") + 1]))
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\n")
        elif "--output" in cmd:
            out = Path(cmd[cmd.index("--output") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("{}")
        return type("R", (), {"returncode": rc, "stderr": "", "stdout": ""})()

    @property
    def uv(self):
        return [(c, e) for c, e in self.calls if c[0] == "/fake/uv"]


@pytest.fixture
def run(monkeypatch):
    r = _Run()
    monkeypatch.setattr(cli.subprocess, "run", r)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/uv" if name == "uv" else None)
    return r


def _tty(monkeypatch, answers: list[str]):
    asked = []
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda self: True})())

    def answer(prompt):
        asked.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr("builtins.input", answer)
    return asked


def _connect(api, repo, *flags):
    return cli.main(["--api", api, "connect", "--repo", str(repo), "--name", "refunds", "--extract", *flags])


def test_interactive_accept_installs_with_the_token_only_in_the_environment(api, repo, run, monkeypatch, capsys,
                                                                             tmp_path):
    _sign_in(api)
    asked = _tty(monkeypatch, ["y"])
    assert _connect(api, repo) == 0
    out = capsys.readouterr().out
    assert "THE LICENCE TEXT" in out and "Accepted the Redberry Extractor Licence" in out
    assert asked == ["Accept the Redberry Extractor Licence for Acme Ltd (version 2026-11)? [y/N] "]
    assert _Api.state["accepted"] and _Api.state["token_posts"] == 1
    venv = tmp_path / "rbhome" / "extractor" / "0.4.1"
    (venv_cmd, venv_env), (own, own_env), (deps, deps_env) = run.uv
    assert venv_cmd[:3] == ["/fake/uv", "venv", str(venv)] and f"{ENV}_PASSWORD" not in venv_env
    assert own[-1] == "redberry-extractor==0.4.1" and "--no-deps" in own
    assert own[own.index("--default-index") + 1] == f"redberry-extractor={INDEX}"
    assert deps[-1] == "redberry-extractor[llm]==0.4.1"
    assert deps[deps.index("--index") + 1] == f"redberry-extractor={INDEX}" and "first-index" in deps
    for env in (own_env, deps_env):
        assert env[f"{ENV}_USERNAME"] == "oauth2accesstoken" and env[f"{ENV}_PASSWORD"] == TOKEN
    assert not any(TOKEN in arg for cmd, _ in run.calls for arg in cmd)         # never in argv
    assert not any(TOKEN in p.read_text(errors="ignore") for p in (tmp_path / "rbhome").rglob("*") if p.is_file())
    extract = run.calls[-1][0]
    assert extract[0] == str(cli._extractor_exe(venv)) and extract[1:5] == ["--repo", str(repo.resolve()),
                                                                           "--agent-id", "refunds"]
    assert (repo / ".redberry" / "manifest.json").exists()


def test_declining_the_licence_installs_nothing(api, repo, run, monkeypatch, capsys):
    _sign_in(api)
    _tty(monkeypatch, ["n"])
    assert _connect(api, repo) == 1
    assert "not accepted" in capsys.readouterr().err
    assert not _Api.state["accepted"] and not run.calls and "token_posts" not in _Api.state


def test_non_interactive_needs_the_licence_flag_and_yes_is_not_enough(api, repo, run, capsys):
    _sign_in(api)
    assert _connect(api, repo, "--yes") == 1
    err = capsys.readouterr().err
    assert "--accept-extractor-licence" in err and "--yes does not" in err
    assert not run.calls and "accept_posts" not in _Api.state
    assert _connect(api, repo, "--json", "--accept-extractor-licence") == 0
    assert _Api.state["accepted"] and _Api.state["accept_posts"] == 1 and len(run.uv) == 3


def test_an_accepted_licence_is_not_asked_again(api, repo, run, capsys):
    _sign_in(api)
    _Api.state["accepted"] = True
    assert _connect(api, repo) == 0
    assert "accept_posts" not in _Api.state and "THE LICENCE TEXT" not in capsys.readouterr().out


def test_an_existing_install_is_reused_without_a_new_token(api, repo, run, capsys, tmp_path):
    _sign_in(api)
    assert _connect(api, repo, "--accept-extractor-licence") == 0
    (repo / ".redberry" / "manifest.json").unlink()
    run.calls.clear()
    assert _connect(api, repo) == 0
    assert _Api.state["token_posts"] == 1 and not run.uv
    assert run.calls[0][0][0] == str(cli._extractor_exe(tmp_path / "rbhome" / "extractor" / "0.4.1"))


def test_not_signed_in_says_to_log_in(api, repo, run, capsys):
    assert _connect(api, repo, "--accept-extractor-licence") == 1
    assert "redberry login" in capsys.readouterr().err and not run.calls


def test_uv_missing_says_how_to_install_it(api, repo, run, monkeypatch, capsys):
    _sign_in(api)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert _connect(api, repo, "--accept-extractor-licence") == 1
    err = capsys.readouterr().err
    assert "astral.sh/uv/install.sh" in err and "pipx install uv" in err
    assert not run.calls and "token_posts" not in _Api.state


@pytest.mark.parametrize("status, says", [(403, "licence is not accepted"), (429, "too many"),
                                          (502, "could not issue")])
def test_token_errors_are_explained(api, repo, run, capsys, status, says):
    _sign_in(api)
    _Api.state["token_status"] = status
    assert _connect(api, repo, "--accept-extractor-licence") == 1
    assert says in capsys.readouterr().err and not run.uv


def test_a_failed_install_never_echoes_the_token(api, repo, monkeypatch, capsys, tmp_path):
    r = _Run(fail_install=True)
    monkeypatch.setattr(cli.subprocess, "run", r)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/uv" if name == "uv" else None)
    _sign_in(api)
    assert _connect(api, repo, "--accept-extractor-licence") == 1
    err = capsys.readouterr().err
    assert "installing the extractor failed" in err and TOKEN not in err and "***" in err
    assert not (tmp_path / "rbhome" / "extractor" / "0.4.1").exists()
    assert cli._installed_extractor() is None


def test_the_extractor_on_path_is_preferred(api, repo, monkeypatch, capsys):
    r = _Run()
    monkeypatch.setattr(cli.subprocess, "run", r)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/" + name)
    assert _connect(api, repo) == 0                      # no session, licence or token needed
    assert [c[0][0] for c in r.calls] == ["redberry-extract"] and "accept_posts" not in _Api.state
