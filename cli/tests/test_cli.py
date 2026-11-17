"""`redberry login | whoami | logout | connect` against a fake Redberry API served over real HTTP.

The fake holds the state the API's /api/cli routes do: a device code someone approves and a session.
Checked: the device-code sign-in (reporting the machine's name) and where the session is kept (0600);
logout revoking the session at Redberry; bare `connect` pointing at the portal's install command;
`--extract` with no extractor installed and no session; and `redberry_cli.instrument`'s edits.
"""

from __future__ import annotations

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from redberry_cli import main as cli


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
        return self.headers.get("Authorization") == "Bearer rbcli_session" and not self.state.get("revoked")

    def do_GET(self):
        if self.path == "/api/cli/whoami" and self._authed():
            return self._reply(200, {"company": {"id": "co_acme", "name": "Acme Ltd"}})
        self._reply(401, {"detail": "Run `redberry login` first"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        s = self.state
        if self.path == "/api/cli/logout" and self._authed():
            s["revoked"] = True
            self.send_response(204)
            self.end_headers()
            return None
        if self.path == "/api/cli/device/start" and s.get("no_cli"):
            return self._reply(404, {"detail": "Not Found"})
        if self.path == "/api/cli/device/start":
            s["machine"] = body.get("machine")
            return self._reply(200, {"device_code": "dev-secret", "user_code": "QJ4K-7WPX",
                                     "verification_uri": "https://portal.example/device",
                                     "verification_uri_complete": "https://portal.example/device?code=QJ4K-7WPX",
                                     "expires_in": 900, "interval": 0})
        if self.path == "/api/cli/device/token":
            s["polls"] = s.get("polls", 0) + 1
            if s["polls"] < 2:
                return self._reply(400, {"error": "authorization_pending"})
            return self._reply(200, {"access_token": "rbcli_session", "token_type": "Bearer", "expires_in": 3600,
                                     "company": {"id": "co_acme", "name": "Acme Ltd"}, "user_email": "dev@acme.com"})
        self._reply(401, {"detail": "Run `redberry login` first"})


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _Api.state = {}
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


def _main(api: str, *argv: str) -> int:
    return cli.main(["--api", api, *argv])


def test_login_whoami(api, monkeypatch, capsys):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert _main(api, "whoami") == 1 and "redberry login" in capsys.readouterr().err   # not signed in yet
    assert _main(api, "login", "--no-browser") == 0
    out = capsys.readouterr().out
    assert "QJ4K-7WPX" in out and "Signed in for Acme Ltd as dev@acme.com" in out
    assert _Api.state["machine"] == cli.socket.gethostname() and _Api.state["machine"] in out
    creds = Path(cli._credentials_path())
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600 and "rbcli_session" in creds.read_text()
    assert _main(api, "whoami") == 0 and "Acme Ltd (dev@acme.com)" in capsys.readouterr().out


def test_logout_revokes_the_session_at_redberry(api, monkeypatch, capsys):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert _main(api, "login", "--no-browser") == 0
    creds = json.loads(Path(cli._credentials_path()).read_text())
    assert _main(api, "logout") == 0 and capsys.readouterr().out.endswith("Signed out\n")
    assert _Api.state["revoked"]
    assert _main(api, "whoami") == 1 and "redberry login" in capsys.readouterr().err
    # the old token no longer works anywhere, not just here
    Path(cli._credentials_path()).write_text(json.dumps(creds))
    assert _main(api, "whoami") == 1
    assert _main(api, "logout") == 0 and "Signed out" in capsys.readouterr().out   # already ended: fine
    assert _main(api, "logout") == 0 and "Not signed in" in capsys.readouterr().out


def test_logout_offline_still_forgets_the_session_and_says_so(api, monkeypatch, capsys):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert _main(api, "login", "--no-browser") == 0
    assert _main("http://127.0.0.1:9", "logout") == 0      # another API: not signed in there
    creds = json.loads(Path(cli._credentials_path()).read_text())
    Path(cli._credentials_path()).write_text(json.dumps({"http://127.0.0.1:9": creds[api]}))
    capsys.readouterr()
    assert _main("http://127.0.0.1:9", "logout") == 0
    assert "could not revoke" in capsys.readouterr().out and json.loads(Path(cli._credentials_path()).read_text()) == {}



def test_login_against_an_api_without_the_cli_says_what_to_do(api, capsys):
    _Api.state["no_cli"] = True
    assert _main(api, "login", "--no-browser") == 1
    err = capsys.readouterr().err
    assert "doesn't support `redberry login`" in err and "REDBERRY_API_URL" in err


def test_the_default_api_is_production_and_the_environment_overrides_it(monkeypatch):
    monkeypatch.delenv("REDBERRY_API_URL", raising=False)
    assert cli.parser().parse_args(["whoami"]).api == cli.DEFAULT_API == "https://api.redberry.sbs"
    monkeypatch.setenv("REDBERRY_API_URL", "https://api.example")
    assert cli.parser().parse_args(["whoami"]).api == "https://api.example"


def test_bare_connect_points_at_the_portals_install_command(api, capsys):
    assert _main(api, "connect") == 0
    out = capsys.readouterr().out
    assert "Redberry portal's install command" in out and "redberry connect --platform <name>" in out
    assert "aws-lambda" in out and "azure-functions" in out
    assert _main(api, "connect", "--json") == 0
    assert json.loads(capsys.readouterr().out)["platforms"] == ["aws-lambda", "azure-functions", "vercel"]


def test_bare_connect_needs_no_sign_in_and_refuses_platform_flags(api, capsys):
    assert _main(api, "connect", "--function", "f") == 1 and "--platform aws-lambda" in capsys.readouterr().err
    assert _main(api, "connect", "--app", "a") == 1 and "--platform azure-functions" in capsys.readouterr().err
    assert _main(api, "connect", "--env-file", "x") == 1 and "--platform vercel" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _main(api, "connect", "--platform", "foundry")


def test_extract_without_the_extractor_or_a_session_says_to_log_in(api, repo, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _main(api, "connect", "--repo", str(repo), "--extract") == 1
    err = capsys.readouterr().err
    assert "redberry login" in err and not (repo / ".redberry").exists()


def test_extract_runs_the_extractor_when_it_is_installed(api, repo, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/redberry-extract")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    ran = []

    def run(cmd, cwd=None, **kw):
        ran.append(cmd)
        out = Path(cmd[cmd.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert _main(api, "connect", "--repo", str(repo), "--name", "refunds", "--extract") == 0
    assert ran[0][:5] == ["redberry-extract", "--repo", str(repo.resolve()), "--agent-id", "refunds"]
    assert (repo / ".redberry" / "manifest.json").exists()
    assert "install command" in capsys.readouterr().out            # then, with no platform, the portal message
    ran.clear()
    assert _main(api, "connect", "--repo", str(repo), "--extract") == 0 and not ran   # a manifest: left alone


def test_connect_refuses_a_folder_with_no_agent_in_it(api, monkeypatch, capsys, tmp_path):
    """Run from a home folder by mistake, the extractor described it (0 tools)."""
    empty = tmp_path / "home"
    (empty / "notes").mkdir(parents=True)
    (empty / "notes" / "todo.py").write_text("print('hello')\n")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/redberry-extract")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _main(api, "connect", "--repo", str(empty), "--extract") == 1
    assert "no agent code found" in capsys.readouterr().err


def test_instrument_proposes_two_lines_where_the_llm_client_is_made(tmp_path):
    from redberry_cli import instrument

    repo = tmp_path / "agent"
    repo.mkdir()
    (repo / "agent.py").write_text('#!/usr/bin/env python\n"""My agent."""\nfrom __future__ import annotations\n\n'
                                   'import os\nfrom openai import OpenAI\n\nclient = OpenAI()\n')
    p = instrument.plan(repo)
    assert p.path == (repo / "agent.py").resolve() and "+import redberry_sdk" in p.diff
    instrument.apply(p)
    lines = (repo / "agent.py").read_text().splitlines()
    i = lines.index(instrument.LINES[0])
    assert lines[:i] == ["#!/usr/bin/env python", '"""My agent."""', "from __future__ import annotations"]
    assert lines[i + 1] == instrument.LINES[1] and lines.index("from openai import OpenAI") > i
    assert instrument.plan(repo).reason.startswith("already instrumented")    # never twice
    (repo / "other.py").write_text("import anthropic\nc = anthropic.Anthropic()\n")
    (repo / "agent.py").write_text("from openai import OpenAI\nclient = OpenAI()\n")
    assert instrument.plan(repo).path is None and "2 files" in instrument.plan(repo).reason
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "no LLM client" in instrument.plan(empty).reason


def test_instrument_sets_temperature_1_on_every_model_call(tmp_path):
    from redberry_cli import instrument

    (tmp_path / "anthropic_agent.py").write_text(
        "import anthropic\nclient = anthropic.Anthropic()\n\n"
        "def turn(msgs):\n    return client.messages.create(\n        model='m',\n        max_tokens=10,\n        messages=msgs,\n    )\n\n"
        "def cool(msgs):\n    return client.messages.create(model='m', temperature=0.2, messages=msgs)\n\n"
        "def body(msgs):\n    return client.messages.create(model='m', extra_body={'top_k': 5}, messages=msgs)\n\n"
        "def var(msgs, t):\n    return client.messages.create(model='m', temperature=t, messages=msgs)\n")
    (tmp_path / "openai_agent.py").write_text(
        "from openai import OpenAI\nc = OpenAI()\nc.chat.completions.create(model='m', messages=[])\n")
    changes = instrument.set_temperature(tmp_path, anthropic_v1=True, anthropic_fixed=False)
    got = {(c.path.name, c.line): (c.before, c.changed) for c in changes}
    assert got == {("anthropic_agent.py", 5): ("unset", True), ("anthropic_agent.py", 12): ("0.2", True),
                   ("anthropic_agent.py", 15): ("unset", True), ("anthropic_agent.py", 18): ("variable", False),
                   ("openai_agent.py", 3): ("unset", True)}
    text = (tmp_path / "anthropic_agent.py").read_text()
    assert '        extra_body={"temperature": 1.0},\n        model=' in text           # on its own line
    assert "temperature=1.0, messages=msgs" in text                                  # 0.2 replaced in place
    assert "extra_body={\"temperature\": 1.0, 'top_k': 5}" in text                   # added to the dict
    assert "temperature=1.0, model='m'" in (tmp_path / "openai_agent.py").read_text()
    compile(text, "a", "exec")
    again = instrument.set_temperature(tmp_path, anthropic_v1=True, anthropic_fixed=False)                 # nothing more to do
    assert not any(c.changed for c in again)
    (tmp_path / "old.py").write_text("import anthropic\nanthropic.Anthropic().messages.create(model='m', messages=[])\n")
    instrument.set_temperature(tmp_path, anthropic_v1=False, anthropic_fixed=False)                         # SDK 0.x: the argument
    assert "temperature=1.0, model='m'" in (tmp_path / "old.py").read_text()


def test_instrument_leaves_anthropic_sdk_2_calls_alone(tmp_path):
    """API 2026-10-21 (the anthropic SDK 2.x default) rejects every sampling parameter
    and fixes temperature at 1.0. Adding one would break every call; one already sent is reported."""
    from redberry_cli import instrument

    src = ("import anthropic\nclient = anthropic.Anthropic()\n"
           "client.messages.create(model='m', max_tokens=10, messages=[])\n"
           "client.messages.create(model='m', extra_body={'temperature': 0.2}, messages=[])\n")
    (tmp_path / "agent.py").write_text(src)
    (tmp_path / "openai_agent.py").write_text(
        "from openai import OpenAI\nc = OpenAI()\nc.chat.completions.create(model='m', messages=[])\n")
    changes = instrument.set_temperature(tmp_path, anthropic_v1=True, anthropic_fixed=True)
    got = {(c.path.name, c.line): (c.before, c.changed, c.provider_fixed) for c in changes}
    assert got == {("agent.py", 3): ("unset", False, True), ("agent.py", 4): ("0.2", False, True),
                   ("openai_agent.py", 3): ("unset", True, False)}
    assert (tmp_path / "agent.py").read_text() == src                                 # untouched
    assert "temperature=1.0" in (tmp_path / "openai_agent.py").read_text()          # others as before


@pytest.mark.parametrize("major, added", [(1, True), (2, False)])
def test_instrument_follows_the_installed_anthropic_major(tmp_path, monkeypatch, major, added):
    from redberry_cli import instrument

    monkeypatch.setattr(instrument, "_anthropic_major", lambda: major)
    (tmp_path / "agent.py").write_text("import anthropic\nanthropic.Anthropic().messages.create(model='m', messages=[])\n")
    instrument.set_temperature(tmp_path)
    assert ('"temperature": 1.0' in (tmp_path / "agent.py").read_text()) is added


@pytest.mark.parametrize("api, label", [("https://api.redberry.sbs", "Redberry prod (https://api.redberry.sbs)"),
                                        ("https://api-dev.redberry.sbs/", "Redberry dev (https://api-dev.redberry.sbs)"),
                                        ("http://127.0.0.1:9", "Redberry custom (http://127.0.0.1:9)")])
def test_every_api_command_says_which_environment_on_stderr(api, label, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["--api", api, "whoami"]) == 1  # not signed in: still says where it would have gone
    err = capsys.readouterr().err
    assert err.splitlines()[0] == label and "not signed in" in err


def test_bare_connect_calls_no_api_and_names_none(capsys):
    assert cli.main(["--api", "https://api-dev.redberry.sbs", "connect", "--json"]) == 0
    assert "Redberry dev" not in capsys.readouterr().err
