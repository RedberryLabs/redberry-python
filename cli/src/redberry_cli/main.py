"""``redberry login | whoami | logout | connect | upgrade``: sign in, then wire an agent to the customer's Redberry
install.

**login** — a device-code sign-in, as ``gh auth login`` does it. The CLI prints a short code and a link;
someone signed in to the Redberry portal for your company opens it and approves the code (it need
not be the engineer). The portal shows them this machine's name and address, so they can tell it is
yours. The CLI then holds a session for that company, in ``~/.config/redberry/credentials.json``
(mode 0600; ``$XDG_CONFIG_HOME`` is respected). It lasts 30 days, and only while whoever approved it
still has access to the company.

**whoami** — the company this machine is signed in for. **logout** — revokes the session at Redberry, then
forgets it here.

**connect** — wires an agent's code so it sends its spans to the customer's Redberry installation, which
the agent's SDK finds from where it runs (no key; on Vercel, the settings the install printed). Agents on a
cloud install are set up from the Redberry portal's install command; ``connect --platform`` covers the
serverless platforms:

* ``--platform aws-lambda``: a Node.js or Python Lambda function (``redberry_cli.aws_lambda``);
* ``--platform azure-functions``: a Node.js v4 or Python v2 function app (``redberry_cli.azure_functions``);
* ``--platform vercel``: a Next.js app on the Vercel AI SDK, whose exporter takes the ``REDBERRY_*`` variables
  the install prints for ``--vercel-team`` (``--env-file``), set on the Vercel project (``redberry_cli.vercel``).

Each shows the diff and asks before it changes a file (``--yes`` applies without asking), sets
``temperature`` 1 on each model call it finds (pricing needs exactly 1), and never deploys.
``--extract`` writes the agent's manifest (``.redberry/manifest.json``) with ``redberry-extract``: the one on
PATH, else one installed with uv into ``~/.redberry/extractor`` once the company's extractor licence is accepted.

**upgrade** — the company's installs and whether each runs Redberry's latest release; with an id, upgrades that
install by running its cloud's install script here with ``--upgrade`` (``redberry_cli.upgrade``).

The API is ``$REDBERRY_API_URL`` or ``--api`` (default ``https://api.redberry.sbs``). Off production, every command
that calls it first says so on stderr (``Redberry dev (…)`` or ``Redberry custom (…)``). Every prompt has a flag
(``--name``, ``--yes``), and ``--json`` prints the result for scripts and coding assistants.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from redberry_cli import _channel
from redberry_cli import _cloud
from redberry_cli import _edits
from redberry_cli._version import __version__

DEFAULT_API = _channel.APIS[_channel.CHANNEL]


class CliError(Exception):
    pass


# ── HTTP and the session ────────────────────────────────────────────────────────────────────


def call(api: str, method: str, path: str, body: dict | None = None, token: str | None = None
         ) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CliError(f"cannot reach {api}: {e}") from None


def _credentials_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "redberry" / "credentials.json"


def _load_credentials() -> dict[str, Any]:
    p = _credentials_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_credentials(creds: dict[str, Any]) -> None:
    _edits.write_private(_credentials_path(), json.dumps(creds, indent=2) + "\n")


def session(api: str) -> dict[str, Any]:
    s = _load_credentials().get(api.rstrip("/"))
    if not s or s.get("expires_at", 0) <= time.time():
        raise CliError("not signed in: run `redberry login`")
    return s


def environment(api: str) -> str:
    """``prod``, ``dev``, or ``custom`` for any other address."""
    api = api.rstrip("/")
    return next((name for name, url in _channel.APIS.items() if url == api), "custom")


def _detail(body: dict[str, Any]) -> str:
    return str(body.get("detail") or body.get("error") or body)



def _cli_version() -> str:
    return __version__


# ── commands ────────────────────────────────────────────────────────────────────────────────


def cmd_login(a, out=print, sleep=time.sleep) -> int:
    api = a.api.rstrip("/")
    machine = _machine_name()
    status, start = call(api, "POST", "/api/cli/device/start", {"machine": machine} if machine else {})
    if status == 404:
        raise CliError(f"{api} doesn't support `redberry login`: check --api / REDBERRY_API_URL")
    if status != 200:
        raise CliError(f"could not start the sign-in ({status}): {_detail(start)}")
    out(f"Open {start['verification_uri']} and enter the code  {start['user_code']}")
    out(f"  or open {start['verification_uri_complete']}")
    out("Anyone signed in to Redberry for your company can approve it"
        + (f"; the portal will show this machine as {machine}." if machine else "."))
    if not a.no_browser:
        with contextlib.suppress(Exception):  # a headless machine: the link is printed
            webbrowser.open(start["verification_uri_complete"])
    deadline = time.time() + start["expires_in"]
    while time.time() < deadline:
        sleep(start["interval"])
        status, body = call(api, "POST", "/api/cli/device/token", {"device_code": start["device_code"]})
        if status == 200:
            creds = _load_credentials()
            creds[api] = {"token": body["access_token"], "company": body["company"],
                          "portal": start.get("verification_uri"),
                          "user_email": body.get("user_email"), "expires_at": time.time() + body["expires_in"]}
            _save_credentials(creds)
            out(f"Signed in for {body['company'].get('name') or body['company']['id']}"
                + (f" as {body['user_email']}" if body.get("user_email") else ""))
            return 0
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "access_denied":
            raise CliError("the sign-in was denied in the portal")
        raise CliError(f"the sign-in did not complete ({error or status}); run `redberry login` again")
    raise CliError("the code expired before it was approved; run `redberry login` again")


def cmd_whoami(a, out=print) -> int:
    s = session(a.api)
    status, body = call(a.api, "GET", "/api/cli/whoami", token=s["token"])
    if status != 200:
        raise CliError(f"{status}: {_detail(body)}")
    company = body["company"]
    out(f"{company.get('name') or company['id']}" + (f" ({s['user_email']})" if s.get("user_email") else ""))
    return 0


def cmd_logout(a, out=print) -> int:
    api = a.api.rstrip("/")
    creds = _load_credentials()
    s = creds.pop(api, None)
    if s is None:
        out("Not signed in")
        return 0
    try:
        status, body = call(api, "POST", "/api/cli/logout", token=s["token"])
        revoked = status in (204, 401)  # 401: already ended at Redberry
    except (CliError, _cloud.SetupError) as e:
        status, body, revoked = None, {"detail": str(e)}, False
    _save_credentials(creds)
    if revoked:
        out("Signed out")
    else:
        out(f"Signed out on this machine, but Redberry could not revoke the session ({status or 'offline'}: "
            f"{_detail(body)}); it ends when it expires, or when whoever approved it loses access.")
    return 0


def _machine_name() -> str | None:
    with contextlib.suppress(OSError):
        return socket.gethostname() or None
    return None


PLATFORMS = ("aws-lambda", "azure-functions", "vercel")

#: What bare ``redberry connect`` says: the install command does the rest.
PORTAL_MESSAGE = ("Agents on a cloud install are set up from the Redberry portal's install command: run it "
                  "where the install lives, and choose the agents there.\n"
                  "For AWS Lambda, Azure Functions and Vercel, wire the agent's code with "
                  "`redberry connect --platform <name>` (" + ", ".join(PLATFORMS) + ").")


def cmd_connect(a, out=print, run=None) -> int:
    platform = getattr(a, "platform", None)
    if platform != "aws-lambda":
        for flag in ("function", "region"):
            if getattr(a, flag, None):
                raise CliError(f"--{flag} is for --platform aws-lambda")
    if platform != "azure-functions":
        for flag in ("app", "resource_group", "subscription"):
            if getattr(a, flag, None):
                raise CliError(f"--{flag.replace('_', '-')} is for --platform azure-functions")
    if platform != "vercel":
        for flag in ("env_file", "no_env"):
            if getattr(a, flag, None):
                raise CliError(f"--{flag.replace('_', '-')} is for --platform vercel")
    if getattr(a, "extract", False):
        repo = Path(a.repo).resolve()
        manifest = Path(a.manifest) if Path(a.manifest).is_absolute() else repo / a.manifest
        if not manifest.exists():
            _extract_manifest(a, repo, manifest, a.name or repo.name, out, run or subprocess.run)
    if platform == "aws-lambda":
        from redberry_cli import aws_lambda  # the function sends its own spans

        return aws_lambda.cmd_connect(a, out=out, run=run)
    if platform == "azure-functions":
        from redberry_cli import azure_functions  # the function app sends its own spans

        return azure_functions.cmd_connect(a, out=out, run=run)
    if platform == "vercel":
        from redberry_cli import vercel  # the deployment sends its own spans

        return vercel.cmd_connect(a, out=out, run=run)
    if a.json:
        out(json.dumps({"platform": None, "message": PORTAL_MESSAGE, "platforms": list(PLATFORMS)}, indent=2))
    else:
        out(PORTAL_MESSAGE)
    return 0


def cmd_upgrade(a, out=print, run=None) -> int:
    from redberry_cli import upgrade  # the install script does the work, in the customer's cloud

    return upgrade.cmd_upgrade(a, out=out, run=run)


_EXTRACT_BACKENDS = (("ANTHROPIC_API_KEY", "Anthropic", []),
                     ("OPENAI_API_KEY", "OpenAI", ["--backend", "openai_compatible", "--base-url",
                                                   "https://api.openai.com/v1", "--model", "gpt-4.1-mini"]))


def _extract_manifest(a, repo: Path, manifest: Path, name: str, out, run) -> None:
    """No manifest: write one with redberry-extract and the LLM key already in the environment. It shows the
    agent's code to that provider, so this asks unless --extract said yes. ``redberry-extract`` on PATH is used
    as it is; otherwise it is installed for this machine (``_install_extractor``)."""
    _require_agent_code(repo)
    found = next(((env, who, extra) for env, who, extra in _EXTRACT_BACKENDS if os.environ.get(env)), None)
    how = f"redberry-extract --repo {repo} --agent-id {name}   (needs ANTHROPIC_API_KEY or OPENAI_API_KEY)"
    if found is None:
        raise CliError(f"no manifest at {manifest}, and no LLM key to write one with. Write it: {how}")
    env, who, extra = found
    if not a.extract:
        if not sys.stdin.isatty() or a.json:
            raise CliError(f"no manifest at {manifest}. Pass --extract to write one with your {env} "
                           f"(the agent's code is sent to {who}), or write it yourself: {how}")
        answer = input(f"No manifest yet. Describe this agent with your {env}? Its code is sent to {who}. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            raise CliError(f"no manifest. Write it yourself: {how}")
    extractor = "redberry-extract" if shutil.which("redberry-extract") else str(_install_extractor(a, out, run))
    out(f"Describing the agent with {who} (redberry-extract) ...")
    cmd = [extractor, "--repo", str(repo), "--agent-id", name, "--output", str(manifest), *extra]
    if run(cmd, cwd=repo).returncode != 0 or not manifest.exists():
        raise CliError(f"redberry-extract did not write {manifest}; run it yourself: {how}")


# ── the extractor: licence, download token, install with uv ─────────────────────────────────

#: The private index's name for uv: its credential is read from ``UV_INDEX_<NAME>_USERNAME`` / ``_PASSWORD``.
EXTRACTOR_INDEX = "redberry-extractor"
_INDEX_ENV = "UV_INDEX_" + EXTRACTOR_INDEX.upper().replace("-", "_")
_INSTALLED = ".redberry-installed"


def _redberry_home() -> Path:
    return Path(os.environ.get("REDBERRY_HOME") or Path.home() / ".redberry")


def _extractor_root() -> Path:
    return _redberry_home() / "extractor"


def _extractor_exe(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "redberry-extract.exe"
    return venv / "bin" / "redberry-extract"


def _installed_extractor() -> Path | None:
    """The install recorded as current, when it is complete."""
    root = _extractor_root()
    with contextlib.suppress(OSError):
        venv = root / (root / "current").read_text(encoding="utf-8").strip()
        if venv.parent == root and (venv / _INSTALLED).exists() and _extractor_exe(venv).exists():
            return venv
    return None


def _install_extractor(a, out, run) -> Path:
    """redberry-extract for this machine: after the company's extractor licence is accepted, installed with uv
    from Redberry's private index into ``~/.redberry/extractor/<version>/`` (``$REDBERRY_HOME``), or the
    install already there. The download token is short-lived and reaches uv only in its environment."""
    try:
        s = session(a.api)
    except CliError:
        raise CliError("the extractor is installed for your company: sign in first with `redberry login`, "
                       "then run connect again") from None
    _accept_licence(a, s, out)
    venv = _installed_extractor()
    if venv is not None:
        return _extractor_exe(venv)
    uv = shutil.which("uv")
    if not uv:
        raise CliError("installing the extractor needs uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` "
                       "or `pipx install uv`, then run connect again")
    status, grant = call(a.api, "POST", "/api/cli/extractor-token", token=s["token"])
    if status != 200:
        raise CliError(_token_error(status, grant))
    secret = grant["access_token"]
    pin = str(grant.get("version_pin") or "").strip()
    spec = "" if not pin else pin if pin[0] in "=<>!~" else f"=={pin}"
    version = "".join(c for c in pin.lstrip("=<>!~ ") if c.isalnum() or c in ".-_+") or "latest"
    root = _extractor_root()
    venv = root / version
    shutil.rmtree(venv, ignore_errors=True)  # an earlier install that did not finish
    root.mkdir(parents=True, exist_ok=True)
    out(f"Installing the Redberry extractor ({version}) into {venv} ...")
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    plain = {k: v for k, v in os.environ.items() if not k.startswith(_INDEX_ENV)}
    # The token goes to uv in its environment only: never in argv (visible to other users in ps), a file or a log.
    authed = {**plain, f"{_INDEX_ENV}_USERNAME": grant.get("username") or "oauth2accesstoken",
              f"{_INDEX_ENV}_PASSWORD": secret}
    private = f"{EXTRACTOR_INDEX}={grant['index_url']}"
    steps = [
        ([uv, "venv", str(venv), "--python", python], plain),
        # the extractor itself from the private index alone, so a package of the same name elsewhere never wins
        ([uv, "pip", "install", "--python", str(venv), "--no-deps", "--default-index", private,
          f"redberry-extractor{spec}"], authed),
        # then its dependencies: the private index first, PyPI as the default
        ([uv, "pip", "install", "--python", str(venv), "--index-strategy", "first-index", "--index", private,
          f"redberry-extractor[llm]{spec}"], authed),
    ]
    for cmd, env in steps:
        try:
            r = run(cmd, capture_output=True, text=True, env=env)
        except OSError as e:
            shutil.rmtree(venv, ignore_errors=True)
            raise CliError(f"could not run uv: {str(e).replace(secret, '***')}") from None
        if r.returncode != 0:
            shutil.rmtree(venv, ignore_errors=True)
            detail = ((r.stderr or "") + (r.stdout or "")).replace(secret, "***").strip()[-2000:]
            raise CliError(f"installing the extractor failed (`{' '.join(cmd[1:3])}`): {detail}")
    if not _extractor_exe(venv).exists():
        shutil.rmtree(venv, ignore_errors=True)
        raise CliError(f"the extractor installed without its command ({_extractor_exe(venv).name})")
    (venv / _INSTALLED).write_text(version + "\n", encoding="utf-8")
    (root / "current").write_text(version + "\n", encoding="utf-8")
    return _extractor_exe(venv)


def _accept_licence(a, s: dict[str, Any], out) -> None:
    """The Redberry Extractor Licence, accepted once per company (and again for a new version). ``--yes`` does
    not accept it: without a terminal, ``--accept-extractor-licence`` does."""
    status, lic = call(a.api, "GET", "/api/cli/extractor-licence", token=s["token"])
    if status == 401:
        raise CliError("your Redberry session has ended: run `redberry login`")
    if status != 200:
        raise CliError(f"could not read the extractor licence ({status}): {_detail(lic)}")
    if lic.get("accepted"):
        return
    company = s.get("company") or {}
    company = company.get("name") or company.get("id") or "your company"
    version = lic.get("version")
    if not getattr(a, "accept_extractor_licence", False):
        if not sys.stdin.isatty() or a.json:
            raise CliError(f"the Redberry Extractor Licence (version {version}) is not yet accepted for {company}. "
                           "Read it in the Redberry portal or run connect in a terminal, and pass "
                           "--accept-extractor-licence to accept it without a prompt (--yes does not)")
        out(lic.get("text") or "")
        answer = input(f"Accept the Redberry Extractor Licence for {company} (version {version})? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            raise CliError("the extractor licence was not accepted; write the manifest by hand instead")
    status, body = call(a.api, "POST", "/api/cli/extractor-licence", {"version": version}, token=s["token"])
    if status == 409:
        raise CliError("the extractor licence changed while you read it: run connect again to see the new version")
    if status != 200:
        raise CliError(f"could not accept the extractor licence ({status}): {_detail(body)}")
    out(f"Accepted the Redberry Extractor Licence (version {version}) for {company}")


def _token_error(status: int, body: dict[str, Any]) -> str:
    detail = body.get("detail")
    message = detail.get("message") if isinstance(detail, dict) else None
    if status == 401:
        return "your Redberry session has ended: run `redberry login`"
    if status == 403:
        return ("the extractor licence is not accepted for your company"
                + (f" ({message})" if message else "") + ": run connect again to accept it")
    if status == 429:
        return "too many extractor downloads for now: try again in a few minutes"
    if status == 502:
        return "Redberry could not issue an extractor download token right now: try again shortly"
    return f"could not get an extractor download token ({status}): {message or _detail(body)}"


def _require_agent_code(repo: Path) -> None:
    """Before anything is described: ``repo`` holds an agent. Run from a home folder by mistake, the
    extractor described it (0 tools, 0 models)."""
    redberry = (Path.home() / ".redberry").resolve()
    if repo == redberry or repo.is_relative_to(redberry):
        raise CliError(f"{repo} is Redberry's own folder: run connect in the agent's folder (or --repo)")
    from redberry_cli._scan import scan

    b = scan(repo)
    if not (b.static_tools or b.tool_candidates or b.model_candidates):
        raise CliError(f"no agent code found in {repo} (no tools or model client): run connect in "
                       "the agent's folder, or pass --repo")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="redberry", description=__doc__.split("\n\n")[0])
    p.add_argument("--api", default=os.environ.get("REDBERRY_API_URL", DEFAULT_API), help="the Redberry API")
    p.add_argument("--version", action="version", version=f"redberry {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    login = sub.add_parser("login", help="sign this machine in for your company")
    login.add_argument("--no-browser", action="store_true", help="print the link only")
    login.set_defaults(fn=cmd_login)
    sub.add_parser("whoami", help="the company this machine is signed in for").set_defaults(fn=cmd_whoami)
    sub.add_parser("logout", help="forget the session").set_defaults(fn=cmd_logout)
    con = sub.add_parser("connect", help="wire an agent's code to send its spans to your Redberry install")
    con.add_argument("--platform", choices=PLATFORMS,
                     help="aws-lambda, azure-functions or vercel (without it: how agents on a cloud install are set up)")
    con.add_argument("--name", help="the agent's name (1 to 64 printable characters; default: the function's or "
                                    "the function app's name; vercel: the Vercel project's); --platform azure-functions: also name=function,... "
                                    "to pick the functions")
    con.add_argument("--repo", default=".", help="the agent's code (default: here)")
    con.add_argument("--manifest", default=".redberry/manifest.json", help="relative to --repo, or absolute")
    con.add_argument("--yes", action="store_true", help="apply the changes without asking")
    con.add_argument("--extract", action="store_true",
                     help="with no manifest, write one with redberry-extract and your LLM key, without asking "
                          "(installs the extractor with uv when it is not on PATH)")
    con.add_argument("--accept-extractor-licence", action="store_true",
                     help="accept the Redberry Extractor Licence for your company without a prompt")
    con.add_argument("--json", action="store_true", help="print the result as JSON")
    az = con.add_argument_group("Azure Functions (--platform azure-functions: a Node.js v4 or Python v2 function app)")
    az.add_argument("--app", help="the function app's name")
    az.add_argument("--resource-group", help="the function app's resource group")
    az.add_argument("--subscription", help="the function app's subscription (default: az's current one)")
    aw = con.add_argument_group("AWS Lambda (--platform aws-lambda: a Node.js or Python function)")
    aw.add_argument("--function", help="the Lambda function's name")
    aw.add_argument("--region", help="the Lambda function's region (default: the aws CLI's)")
    vc = con.add_argument_group("Vercel (--platform vercel: a Next.js app on the Vercel AI SDK)")
    vc.add_argument("--env-file", help="the REDBERRY_* lines the install printed for --vercel-team, saved to a file "
                                       "(else: the same variables in this environment)")
    vc.add_argument("--no-env", action="store_true",
                    help="print the REDBERRY_* variables instead of setting them with the Vercel CLI")
    con.set_defaults(fn=cmd_connect)
    up = sub.add_parser("upgrade", help="your installs' releases; with an id, upgrade that install to the latest")
    up.add_argument("installation", nargs="?", help="the installation to upgrade (without it: list them)")
    up.add_argument("--yes", action="store_true", help="upgrade without asking")
    up.add_argument("--force", action="store_true", help="run the upgrade even when the install is up to date")
    up.add_argument("--json", action="store_true", help="list the installations as JSON")
    up.set_defaults(fn=cmd_upgrade)
    return p


def main(argv: list[str] | None = None) -> int:
    # Line by line even when stdout is not a terminal (ssh -c, a log file): `login` prints the code a
    # person must approve, then waits, so a buffered stdout would show nothing until it is too late.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)
    args = parser().parse_args(argv)
    env = environment(args.api)
    if env != "prod" and (args.cmd != "connect" or args.extract):  # off production, say where, on stderr
        print(f"Redberry {env} ({args.api.rstrip('/')})", file=sys.stderr)
    try:
        return args.fn(args)
    except (CliError, _cloud.SetupError) as e:
        print(f"redberry: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
