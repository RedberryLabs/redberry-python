"""``redberry upgrade``: the company's Redberry installs, whether each runs Redberry's latest release, and the
upgrade of one.

Without an id it lists the installs of the signed-in company: where each is, the release it last reported and
whether a newer one is out. With an id it upgrades that install: it fetches the cloud's install script from the
Redberry portal and runs it here with ``--upgrade``, through the cloud's CLI as it is signed in on this machine
(``gcloud``, ``aws`` or ``az``). The script keeps every choice the install was made with, asks nothing, needs no
install token, and reports the new release to Redberry. It is the same command the portal shows, which can also
be pasted into the cloud's own shell.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from redberry_cli import _cloud
from redberry_cli.main import CliError, _detail, call, session

#: The cloud's CLI the install script runs, and where to get it.
CLOUD_CLIS = {
    "gcp": ("gcloud", "https://cloud.google.com/sdk/docs/install, then `gcloud auth login`"),
    "aws": ("aws", "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html, then "
                   "`aws configure` or `aws sso login`"),
    "azure": ("az", "https://learn.microsoft.com/cli/azure/install-azure-cli, then `az login`"),
}


def installations(api: str, token: str) -> list[dict[str, Any]]:
    status, body = call(api, "GET", "/api/cli/installations", token=token)
    if status == 401:
        raise CliError("your Redberry session has ended: run `redberry login`")
    if status == 404:
        raise CliError(f"{api} does not list installations yet: check --api / REDBERRY_API_URL")
    if status != 200:
        raise CliError(f"could not list your installations ({status}): {_detail(body)}")
    return list(body.get("installations") or [])


def where(i: dict[str, Any]) -> str:
    if i.get("cloud") == "aws":
        return f"account {i.get('account_id') or '?'}"
    if i.get("cloud") == "azure":
        return f"subscription {i.get('subscription_id') or '?'}"
    return f"project {i.get('project_id') or '?'}"


def state(i: dict[str, Any]) -> str:
    if i.get("status") != "active":
        return str(i.get("status") or "unknown")
    if i.get("behind") is True:
        return "update available"
    if i.get("behind") is False:
        return "up to date"
    return "release not known" if not i.get("release") else "latest not known"


def _reported(i: dict[str, Any]) -> str:
    release = i.get("release") or {}
    if not release:
        return "no release reported"
    when = release.get("reported_at")
    day = time.strftime("%Y-%m-%d", time.gmtime(when / 1000)) if isinstance(when, (int, float)) else None
    return f"{release.get('tag') or '?'}" + (f", reported {day}" if day else "")


def cmd_upgrade(a, out=print, run=None, fetch=None) -> int:
    s = session(a.api)
    rows = installations(a.api, s["token"])
    if not a.installation:
        return _list(a, rows, out)
    found = [i for i in rows if i.get("id") == a.installation]
    if not found:
        known = ", ".join(i["id"] for i in rows if i.get("id")) or "none"
        raise CliError(f"no installation {a.installation} for your company (yours: {known})")
    i = found[0]
    hint = i.get("upgrade")
    if i.get("status") != "active" or not hint:
        raise CliError(f"{i['id']} is {i.get('status') or 'not active'}: a stopped or revoked install is not "
                       "upgraded; install again with a new command from the Redberry portal")
    if i.get("behind") is False and not a.force:
        out(f"{i['id']} is up to date ({_reported(i)}); --force runs the upgrade anyway")
        return 0
    return _run_upgrade(a, i, hint, out, run or subprocess.run, fetch or _fetch)


def _list(a, rows: list[dict[str, Any]], out) -> int:
    if a.json:
        out(json.dumps({"installations": rows}, indent=2))
        return 0
    if not rows:
        out("No Redberry installs for your company yet: the Redberry portal gives the install command.")
        return 0
    for i in rows:
        out(f"{i.get('id')}  {i.get('cloud')}  {where(i)}  {i.get('region') or '?'}  "
            f"{state(i)}  ({_reported(i)})")
    behind = [i for i in rows if i.get("behind") is True and i.get("upgrade")]
    if behind:
        out("")
        out("Upgrade one with `redberry upgrade <id>`, here, signed in to its cloud; or run its command in the "
            "cloud's shell:")
        for i in behind:
            out(f"  {i['id']}: {i['upgrade']['command']}")
    return 0


def _run_upgrade(a, i: dict[str, Any], hint: dict[str, Any], out, run, fetch) -> int:
    cloud = i.get("cloud")
    if cloud not in CLOUD_CLIS:
        raise CliError(f"{i['id']}: no upgrade for cloud {cloud!r} in this version of the CLI")
    elsewhere = f"or run it in the cloud's shell: {hint['command']}"
    bash = shutil.which("bash")
    if not bash:
        raise CliError(f"the upgrade runs a bash script and there is no bash here; {elsewhere}")
    name, how = CLOUD_CLIS[cloud]
    binary = {"aws": _cloud.aws_binary, "azure": _cloud.az_binary}.get(cloud, lambda: shutil.which("gcloud"))()
    if not binary or not shutil.which(binary):
        raise CliError(f"the upgrade runs the {name} CLI, which is not installed: {how}; {elsewhere}")
    if cloud == "aws":
        _check_aws_account(i, run, elsewhere)
    args = [str(x) for x in hint.get("args") or []]
    if "--upgrade" not in args:
        args.append("--upgrade")
    if i.get("behind") is None:
        out(f"{i['id']}: its release is not known yet ({_reported(i)}); the upgrade takes the latest anyway")
    if not a.yes:
        if not sys.stdin.isatty():
            raise CliError("the upgrade changes your cloud install: pass --yes to run it without a prompt")
        answer = input(f"Upgrade {i['id']} ({cloud}, {where(i)}, {i.get('region')}) with {name} as signed in "
                       f"here, running Redberry's install script with {' '.join(args)}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            out("Not upgraded")
            return 1
    script = fetch(hint["script_url"])
    with tempfile.TemporaryDirectory(prefix="redberry-upgrade-") as tmp:
        path = Path(tmp) / f"install-{cloud}.sh"
        path.write_bytes(script)
        out(f"Upgrading {i['id']} with {hint['script_url']} {' '.join(args)}")
        try:
            code = run([bash, str(path), *args]).returncode
        except OSError as e:
            raise CliError(f"could not run bash: {e}") from None
    if code != 0:
        raise CliError(f"the upgrade did not finish (exit {code}): the script's output above says why. Running it "
                       "again is safe")
    out(f"Upgraded {i['id']}. `redberry upgrade` shows the release it reported.")
    return 0


def _check_aws_account(i: dict[str, Any], run, elsewhere: str) -> None:
    """The AWS script installs into the account the aws CLI is signed in to: it must be the install's."""
    want = i.get("account_id")
    r = _cloud.aws(run, ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"])
    if r.returncode != 0:
        raise CliError(f"the aws CLI is not signed in ({(r.stderr or '').strip()[-300:]}): `aws configure` or "
                       f"`aws sso login`; {elsewhere}")
    have = (r.stdout or "").strip()
    if want and have != want:
        raise CliError(f"the aws CLI is signed in to account {have}, and {i['id']} is in account {want}: sign in "
                       f"to {want} (AWS_PROFILE), {elsewhere}")


def _fetch(url: str) -> bytes:
    """The install script, over https (http only from this machine, for tests)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")):
        raise CliError(f"refusing to fetch the install script over {parts.scheme or 'no scheme'}: {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CliError(f"could not fetch the install script {url}: {e}") from None
    if not data.startswith(b"#!"):
        raise CliError(f"{url} is not a shell script")
    return data
