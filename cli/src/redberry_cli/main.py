"""``redberry login | whoami | logout | connect``: sign in, then wire an agent to the customer's Redberry install.

**login** — a device-code sign-in, as ``gh auth login`` does it. The CLI prints a short code and a link;
someone signed in to the Redberry portal for your company opens it and approves the code (it need
not be the engineer). The portal shows them this machine's name and address, so they can tell it is
yours. The CLI then holds a session for that company, in ``~/.config/redberry/credentials.json``
(mode 0600; ``$XDG_CONFIG_HOME`` is respected). It lasts 30 days, and only while whoever approved it
still has access to the company.

**whoami** — the company this machine is signed in for. **logout** — revokes the session at Redberry, then
forgets it here.

**connect** — wires an agent's code so it sends its spans to the customer's Redberry installation, which
the agent's SDK finds from where it runs (no key, no setting). Agents on a cloud install are set up from
the Redberry portal's install command; ``connect --platform`` covers the serverless platforms:

* ``--platform aws-lambda``: a Node.js or Python Lambda function (``redberry_cli.aws_lambda``);
* ``--platform azure-functions``: a Node.js v4 or Python v2 function app (``redberry_cli.azure_functions``).

Each shows the diff and asks before it changes a file (``--yes`` applies without asking), sets
``temperature`` 1 on each model call it finds (pricing needs exactly 1), and never deploys.
``--extract`` writes the agent's manifest (``.redberry/manifest.json``) with ``redberry-extract`` when that
is installed.

**setup-azure** and **setup-aws** make an Azure Blob container (immutable) or an S3 bucket (Object Lock)
that a function app or Lambda function writes to as its own identity, with no key
(``redberry_cli.azure_setup``, ``redberry_cli.aws_setup``).

The API is ``$REDBERRY_API_URL`` or ``--api`` (default ``https://api.redberry.sbs``). Every prompt has a
flag (``--name``, ``--yes``), and ``--json`` prints the result for scripts and coding assistants.
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
from redberry_cli import _edits
from redberry_cli import aws_setup
from redberry_cli import azure_setup
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


def _detail(body: dict[str, Any]) -> str:
    return str(body.get("detail") or body.get("error") or body)



def _cli_version() -> str:
    return __version__


# ── commands ────────────────────────────────────────────────────────────────────────────────


def cmd_login(a, out=print, sleep=time.sleep) -> int:
    api = a.api.rstrip("/")
    machine = _machine_name()
    out(f"Redberry: {api}")
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
    except CliError as e:
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


PLATFORMS = ("aws-lambda", "azure-functions")

#: What bare ``redberry connect`` says: the install command does the rest.
PORTAL_MESSAGE = ("Agents on a cloud install are set up from the Redberry portal's install command: run it "
                  "where the install lives, and choose the agents there.\n"
                  "For AWS Lambda and Azure Functions, wire the agent's code with "
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
    if a.json:
        out(json.dumps({"platform": None, "message": PORTAL_MESSAGE, "platforms": list(PLATFORMS)}, indent=2))
    else:
        out(PORTAL_MESSAGE)
    return 0


_EXTRACT_BACKENDS = (("ANTHROPIC_API_KEY", "Anthropic", []),
                     ("OPENAI_API_KEY", "OpenAI", ["--backend", "openai_compatible", "--base-url",
                                                   "https://api.openai.com/v1", "--model", "gpt-4.1-mini"]))


def _extract_manifest(a, repo: Path, manifest: Path, name: str, out, run) -> None:
    """No manifest: write one with redberry-extract and the LLM key already in the environment, when
    redberry-extract is installed. It shows the agent's code to that provider, so this asks unless --extract
    said yes."""
    _require_agent_code(repo)
    if not shutil.which("redberry-extract"):
        raise CliError(f"no manifest at {manifest}, and the extractor (redberry-extract) is not available in this "
                       "release. Write the manifest by hand; the tools and model calls connect's own scan finds "
                       "(redberry_cli._scan) are the place to start.")
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
    out(f"Describing the agent with {who} (redberry-extract) ...")
    cmd = ["redberry-extract", "--repo", str(repo), "--agent-id", name, "--output", str(manifest), *extra]
    if run(cmd, cwd=repo).returncode != 0 or not manifest.exists():
        raise CliError(f"redberry-extract did not write {manifest}; run it yourself: {how}")


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
                     help="aws-lambda or azure-functions (without it: how agents on a cloud install are set up)")
    con.add_argument("--name", help="the agent's name (1 to 64 printable characters; default: the function's or "
                                    "the function app's name); --platform azure-functions: also name=function,... "
                                    "to pick the functions")
    con.add_argument("--repo", default=".", help="the agent's code (default: here)")
    con.add_argument("--manifest", default=".redberry/manifest.json", help="relative to --repo, or absolute")
    con.add_argument("--yes", action="store_true", help="apply the changes without asking")
    con.add_argument("--extract", action="store_true",
                     help="with no manifest, write one with redberry-extract (when installed) and your LLM key, "
                          "without asking")
    con.add_argument("--json", action="store_true", help="print the result as JSON")
    az = con.add_argument_group("Azure Functions (--platform azure-functions: a Node.js v4 or Python v2 function app)")
    az.add_argument("--app", help="the function app's name")
    az.add_argument("--resource-group", help="the function app's resource group")
    az.add_argument("--subscription", help="the function app's subscription (default: az's current one)")
    aw = con.add_argument_group("AWS Lambda (--platform aws-lambda: a Node.js or Python function)")
    aw.add_argument("--function", help="the Lambda function's name")
    aw.add_argument("--region", help="the Lambda function's region (default: the aws CLI's)")
    con.set_defaults(fn=cmd_connect)
    azs = sub.add_parser("setup-azure", help="an Azure Blob container (immutable) a Vercel project, a function app "
                                             "or the bundle's sink writes to, as a managed identity (no key)")
    azs.add_argument("--resource-group", required=True, help="the resource group (created if it is missing)")
    azs.add_argument("--location", required=True, help="the Azure region, e.g. westeurope or eastus")
    azs.add_argument("--account", required=True,
                     help="the storage account (3-24 lowercase letters and digits; created, StorageV2, no shared keys)")
    azs.add_argument("--container", help=f"the container (default {azure_setup.DEFAULT_CONTAINER}; not with "
                                         "--evidence, which makes valp-evidence, valp-payloads and valp-index)")
    azs.add_argument("--retention", default=azure_setup.DEFAULT_RETENTION,
                     help="keep every blob at least this long: a number of days or years, e.g. 7y (default) or 1d "
                          "for a test; Azure's shortest is one day")
    azs.add_argument("--lock", action="store_true",
                     help="lock the immutability policy (IRREVERSIBLE: for production, once the setup is proven)")
    azs.add_argument("--identity", help="the user-assigned managed identity (default redberry-trace-writer; with "
                                        "--evidence redberry-valp-sink)")
    azs.add_argument("--subscription", help="the subscription (default: az's current one)")
    azs.add_argument("--sku", help="the storage account's SKU (default: az's, Standard_RAGRS)")
    who = azs.add_argument_group("what writes to it (one of)")
    who.add_argument("--vercel-team", help="a Vercel project: the team's slug (OIDC issuer)")
    who.add_argument("--vercel-project", help="with --vercel-team: the project's name (default: .vercel/project.json)")
    who.add_argument("--function-app", help="an Azure function app: its name (the identity is assigned to it)")
    who.add_argument("--app-resource-group", help="with --function-app: its resource group (default --resource-group)")
    who.add_argument("--evidence", action="store_true",
                     help="the bundle's VALP sink: three containers, the evidence one immutable")
    azs.add_argument("--reader-for-me", action="store_true",
                     help="also give you (the signed-in user) Storage Blob Data Reader on the container(s)")
    azs.add_argument("--repo", default=".", help="the app's folder, for .vercel/project.json and the record")
    azs.add_argument("--print", action="store_true", help="print the az commands instead of running them")
    azs.set_defaults(fn=azure_setup.cmd_setup_azure)
    aws = sub.add_parser("setup-aws", help="an S3 bucket (Object Lock) a Vercel project, a Lambda function or the "
                                          "bundle's sink writes to, with no long-lived key")
    aws.add_argument("--region", required=True, help="the bucket's region, e.g. eu-west-1")
    aws.add_argument("--bucket", help="the bucket to create (s3://… or a name; S3 names are global); not with "
                                      "--evidence")
    aws.add_argument("--prefix", help="with --evidence: the buckets' prefix, e.g. acme-valp makes acme-valp-evidence, "
                                      "acme-valp-payloads and acme-valp-index")
    aws.add_argument("--retention", default=aws_setup.DEFAULT_RETENTION,
                     help="Object Lock's default retention: a number of days or years, e.g. 7y (default) or 1d for a "
                          "test")
    aws.add_argument("--lock", action="store_true",
                     help="COMPLIANCE mode (IRREVERSIBLE: nobody can delete an object before it expires); default "
                          "GOVERNANCE")
    aws.add_argument("--yes", action="store_true", help="with --lock: do not ask before locking")
    aws.add_argument("--profile", help="the aws CLI profile to use (default: its default)")
    who_aws = aws.add_argument_group("what writes to it (one of)")
    who_aws.add_argument("--vercel-team", help="a Vercel project: the team's slug (OIDC issuer)")
    who_aws.add_argument("--vercel-project", help="with --vercel-team: the project's name (default: .vercel/project.json)")
    who_aws.add_argument("--role", help="the IAM role to create (default redberry-vercel-<project>, or with --evidence "
                                        f"{aws_setup.SINK_ROLE})")
    who_aws.add_argument("--function", help="a Lambda function: its name (its execution role gets the policy)")
    who_aws.add_argument("--function-region", help="with --function: its region (default --region)")
    who_aws.add_argument("--evidence", action="store_true",
                         help="the bundle's VALP sink: three buckets, the evidence one with Object Lock")
    aws.add_argument("--repo", default=".", help="the app's folder, for .vercel/project.json and the record")
    aws.add_argument("--print", action="store_true", help="print the aws commands instead of running them")
    aws.set_defaults(fn=aws_setup.cmd_setup_aws)
    return p


def main(argv: list[str] | None = None) -> int:
    # Line by line even when stdout is not a terminal (ssh -c, a log file): `login` prints the code a
    # person must approve, then waits, so a buffered stdout would show nothing until it is too late.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except (CliError, aws_setup.SetupError) as e:
        print(f"redberry: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
