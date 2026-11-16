"""The ``aws`` and ``az`` command-line tools, as ``connect --platform aws-lambda | azure-functions`` runs them
to read a function: where each binary is, a runner that says how to install it when it is missing, and the
AWS region check."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


class SetupError(Exception):
    pass


_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d")


def aws_binary() -> str:
    """``$AWS``, else ``aws`` on PATH, else the CLI in ``~/.aws-cli-venv`` (a pip install of awscli)."""
    if os.environ.get("AWS"):
        return os.environ["AWS"]
    found = shutil.which("aws")
    if found:
        return found
    venv = Path.home() / ".aws-cli-venv" / "bin" / "aws"
    return str(venv) if venv.exists() else "aws"


def aws(run, cmd: list[str], **kw):
    """Run ``cmd`` (written as ``["aws", …]``) with the resolved aws binary."""
    try:
        return run([aws_binary(), *cmd[1:]], capture_output=True, text=True, **kw)
    except OSError:
        raise SetupError("the AWS CLI (aws) is not installed: https://docs.aws.amazon.com/cli/latest/userguide/"
                         "getting-started-install.html, then `aws configure` or `aws sso login`") from None


def region(value: str | None, flag: str = "--region") -> str:
    if not _REGION.fullmatch(value or ""):
        raise SetupError(f"{flag}: an AWS region, e.g. eu-west-1 or us-east-1")
    return value  # type: ignore[return-value]


def az_binary() -> str:
    """``$AZ``, else ``az`` on PATH, else the CLI in ``~/.azure-cli-venv`` (a pip install of azure-cli)."""
    if os.environ.get("AZ"):
        return os.environ["AZ"]
    found = shutil.which("az")
    if found:
        return found
    venv = Path.home() / ".azure-cli-venv" / "bin" / "az"
    return str(venv) if venv.exists() else "az"


def az(run, cmd: list[str], **kw):
    """Run ``cmd`` (written as ``["az", …]``) with the resolved az binary."""
    try:
        return run([az_binary(), *cmd[1:]], capture_output=True, text=True, **kw)
    except OSError:
        raise SetupError("the Azure CLI (az) is not installed: https://learn.microsoft.com/cli/azure/install-azure-cli,"
                         " then `az login`") from None
