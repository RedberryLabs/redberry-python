"""`redberry upgrade`: the company's installs and their release, and the upgrade of one (its cloud's install script
run here with --upgrade), against a fake Redberry API over real HTTP and a fake subprocess.run."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar

import pytest

from redberry_cli import main as cli
from redberry_cli import upgrade

SCRIPT = b"#!/usr/bin/env bash\necho upgrading\n"


class _Api(BaseHTTPRequestHandler):
    state: ClassVar[dict] = {}

    def log_message(self, *args):  # quiet
        pass

    def _reply(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/install/"):
            self.state["fetched"] = self.path
            return self._reply(200, SCRIPT, "text/x-shellscript")
        if self.path == "/api/cli/installations":
            if self.headers.get("Authorization") != "Bearer rbcli_session":
                return self._reply(401, b'{"detail": "Run `redberry login` first"}')
            return self._reply(200, json.dumps({"installations": self.state["rows"]}).encode())
        self._reply(404, b'{"detail": "Not Found"}')


def _row(api: str, id: str = "inst_gcp", cloud: str = "gcp", behind=True, status: str = "active", **kw) -> dict:
    args = {"gcp": ["--project", "acme", "--region", "europe-west2", "--upgrade"],
            "aws": ["--region", "eu-west-2", "--upgrade"],
            "azure": ["--subscription", "s-1", "--region", "uksouth", "--upgrade"]}[cloud]
    url = f"{api}/install/{cloud}.sh"
    return {"id": id, "cloud": cloud, "project_id": "acme" if cloud == "gcp" else None,
            "account_id": "123456789012" if cloud == "aws" else None,
            "subscription_id": "s-1" if cloud == "azure" else None,
            "region": args[args.index("--region") + 1], "status": status, "last_seen_at": 1,
            "release": {"tag": "stable", "images": {"sink": "sha256:aa"}, "reported_at": 1_763_380_800_000},
            "latest": {"tag": "stable", "images": {"sink": "sha256:bb"}}, "behind": behind,
            "upgrade": None if status != "active" else
            {"script_url": url, "args": args, "command": f"curl -fsSL {url} | bash -s -- {' '.join(args)}"}, **kw}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _Api.state = {"rows": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    cli._save_credentials({url: {"token": "rbcli_session", "company": {"id": "co_acme", "name": "Acme Ltd"},
                                 "expires_at": time.time() + 3600}})
    # the cloud CLIs and bash are "installed"
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name.rsplit('/', 1)[-1]}")
    monkeypatch.setattr(upgrade._cloud, "aws_binary", lambda: "aws")
    yield url
    server.shutdown()


class _Run:
    def __init__(self, code: int = 0, account: str = "123456789012"):
        self.calls: list[list[str]] = []
        self.code, self.account = code, account
        self.script: bytes | None = None

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[1:3] == ["sts", "get-caller-identity"]:
            return SimpleNamespace(returncode=0, stdout=self.account + "\n", stderr="")
        with open(cmd[1], "rb") as f:  # the script, as fetched, in a temporary file
            self.script = f.read()
        return SimpleNamespace(returncode=self.code, stdout=None, stderr=None)


def _main(api: str, *argv: str, run=None) -> int:
    a = cli.parser().parse_args(["--api", api, "upgrade", *argv])
    return cli.cmd_upgrade(a, run=run)


def test_lists_the_installs_and_the_command_for_those_behind(api, capsys):
    _Api.state["rows"] = [_row(api), _row(api, "inst_aws", "aws", behind=False),
                          _row(api, "inst_az", "azure", behind=None, release=None),
                          _row(api, "inst_old", status="stopped")]
    assert _main(api) == 0
    out = capsys.readouterr().out
    assert "inst_gcp  gcp  project acme  europe-west2  update available  (stable, reported 2025-11-17)" in out
    assert "inst_aws  aws  account 123456789012  eu-west-2  up to date" in out
    assert "inst_az  azure  subscription s-1  uksouth  release not known  (no release reported)" in out
    assert "inst_old  gcp  project acme  europe-west2  stopped" in out
    assert f"inst_gcp: curl -fsSL {api}/install/gcp.sh | bash -s -- --project acme --region europe-west2 --upgrade" in out
    assert "inst_aws:" not in out and "inst_az:" not in out


def test_json_lists_what_the_api_returned(api, capsys):
    _Api.state["rows"] = [_row(api)]
    assert _main(api, "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"installations": _Api.state["rows"]}


def test_needs_a_session(api, capsys, monkeypatch):
    cli._save_credentials({})
    with pytest.raises(cli.CliError, match="redberry login"):
        _main(api)


def test_upgrades_one_by_running_its_script_with_upgrade(api, capsys):
    _Api.state["rows"] = [_row(api)]
    run = _Run()
    assert _main(api, "inst_gcp", "--yes", run=run) == 0
    assert _Api.state["fetched"] == "/install/gcp.sh"
    (cmd,) = run.calls
    assert cmd[0] == "/usr/bin/bash" and cmd[2:] == ["--project", "acme", "--region", "europe-west2", "--upgrade"]
    assert run.script == SCRIPT
    assert "Upgraded inst_gcp" in capsys.readouterr().out


def test_an_install_that_is_up_to_date_is_left_alone_unless_forced(api, capsys):
    _Api.state["rows"] = [_row(api, behind=False)]
    run = _Run()
    assert _main(api, "inst_gcp", "--yes", run=run) == 0
    assert run.calls == [] and "up to date" in capsys.readouterr().out
    assert _main(api, "inst_gcp", "--yes", "--force", run=run) == 0
    assert len(run.calls) == 1


def test_a_stopped_install_is_not_upgraded(api):
    _Api.state["rows"] = [_row(api, status="stopped")]
    with pytest.raises(cli.CliError, match="stopped or revoked"):
        _main(api, "inst_gcp", "--yes", run=_Run())


def test_an_unknown_id_names_the_companys_installs(api):
    _Api.state["rows"] = [_row(api)]
    with pytest.raises(cli.CliError, match="yours: inst_gcp"):
        _main(api, "inst_nope", "--yes", run=_Run())


def test_without_a_terminal_it_needs_yes(api, monkeypatch):
    _Api.state["rows"] = [_row(api)]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    run = _Run()
    with pytest.raises(cli.CliError, match="--yes"):
        _main(api, "inst_gcp", run=run)
    assert run.calls == []


def test_aws_checks_the_signed_in_account_first(api):
    _Api.state["rows"] = [_row(api, "inst_aws", "aws")]
    run = _Run(account="999999999999")
    with pytest.raises(cli.CliError, match="signed in to account 999999999999.*123456789012"):
        _main(api, "inst_aws", "--yes", run=run)
    assert len(run.calls) == 1  # sts only: the script never ran
    run = _Run()
    assert _main(api, "inst_aws", "--yes", run=run) == 0
    assert run.calls[-1][2:] == ["--region", "eu-west-2", "--upgrade"]


def test_a_missing_cloud_cli_gives_the_cloud_shell_command(api, monkeypatch):
    _Api.state["rows"] = [_row(api, "inst_az", "azure")]
    monkeypatch.setattr(upgrade._cloud, "az_binary", lambda: "az")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: None if name == "az" else f"/usr/bin/{name}")
    with pytest.raises(cli.CliError, match=r"az CLI, which is not installed.*bash -s -- --subscription s-1"):
        _main(api, "inst_az", "--yes", run=_Run())


def test_a_failed_script_is_reported(api):
    _Api.state["rows"] = [_row(api)]
    with pytest.raises(cli.CliError, match="exit 3"):
        _main(api, "inst_gcp", "--yes", run=_Run(code=3))


def test_the_script_is_fetched_over_https_only():
    with pytest.raises(cli.CliError, match="refusing"):
        upgrade._fetch("http://claims.example/install/gcp.sh")
