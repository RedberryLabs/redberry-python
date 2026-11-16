"""``redberry setup-aws``: an S3 bucket with Object Lock that stored runs or evidence are written to, with no
long-lived key in the writer.

The writer holds no access key: a Vercel project assumes an IAM role with its OIDC token (STS
``AssumeRoleWithWebIdentity``), a Lambda function writes as its execution role, and the bundle's VALP sink
as the EC2 instance's role (instance profile). This runs (or, with ``--print``, prints) the ``aws``
commands:

1. the bucket (``--bucket``, ``--region``), with Object Lock enabled at creation (which turns versioning
   on: Object Lock needs it), Block Public Access on, SSE-S3 default encryption (AES256) and
   ``BucketOwnerEnforced`` object ownership (no ACLs);
2. a bucket policy that refuses any ``s3:PutObject`` that is not a conditional create (``If-None-Match:
   *``): Object Lock protects versions, not keys, so without it a writer could put a new current version
   over a stored run;
3. the bucket's default retention (``--retention``, days or years, default 7y): every new object version
   can't be overwritten or deleted until it is that old. GOVERNANCE mode (a user with
   ``s3:BypassGovernanceRetention`` can still remove a test object) unless ``--lock``: COMPLIANCE mode,
   which nobody, the account's root user included, can shorten or remove until it expires. **That
   cannot be undone**: use it once the setup is proven;
4. for the target:

   * ``--vercel-team T [--vercel-project P]``: the IAM OIDC provider ``oidc.vercel.com/<team>`` (audience
     ``https://vercel.com/<team>``; created if missing), the role ``redberry-vercel-<project>`` that only
     the project's production deployments can assume (``sub = owner:<team>:project:<project>:
     environment:production``), and an inline policy on it allowing ``s3:PutObject`` and ``s3:GetObject``
     on ``arn:aws:s3:::<bucket>/redberry/*`` (and ``s3:ListBucket`` for that prefix), and nothing else: no
     delete, no retention change, no governance bypass;
   * ``--function NAME``: the same inline policy on the Lambda function's execution role (found with
     ``aws lambda get-function-configuration``);
   * ``--evidence --prefix P``: three buckets for the bundle's VALP sink, ``P-evidence`` (Object Lock),
     ``P-payloads`` and ``P-index`` (no Object Lock, versioning never enabled: the sink rewrites the index
     and erasure replaces payload packs in place, so no old version may survive), and the role
     ``redberry-valp-sink`` (trusted by EC2, with an instance profile of the same name) allowed to create and
     read evidence, and to create, read, replace and delete payloads and index, and list all three. Attaching the
     instance profile to the machine the bundle runs on is printed, as is raising the instance's metadata
     hop limit to 2, which the sink's container needs to reach the instance's credentials (IMDSv2). Off
     AWS, an IAM user with that policy and an access key in the bundle's ``.env`` is yours to make.

Each step is idempotent: what is already there is left as it is (a bucket's COMPLIANCE retention is never
turned back into GOVERNANCE). What it made is recorded in ``.redberry/aws.json`` (names and ARNs only, no
secret), which ``redberry connect --store s3`` reads for the bucket's region and the role.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from redberry_cli._oidc import SetupError, oidc_audience, oidc_subject

RECORD = Path(".redberry") / "aws.json"
DEFAULT_RETENTION = "7y"
POLICY_NAME = "redberry-s3-write"
SINK_ROLE = "redberry-valp-sink"
SINK_POLICY_NAME = "redberry-valp-sink-s3"
#: The roles the bundle's three buckets play (bundle.BUCKET_ROLES), and which of them has Object Lock.
EVIDENCE_ROLES = ("evidence", "payloads", "index")
LOCKED_ROLES = ("evidence",)
#: Where the stored runs go in the bucket (SPEC §5: redberry/<agent>/<date>/<trace>.json.gz).
RUNS_PREFIX = "redberry/"
MAX_RETENTION_DAYS = 36500
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
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


def bucket_name(value: str) -> str:
    """``s3://<bucket>`` or ``<bucket>`` as the bucket's name: 3-63 lowercase letters, digits and hyphens
    (no dots: the stores use virtual-hosted URLs, which a dotted name breaks for TLS)."""
    name = (value or "").strip().removeprefix("s3://").strip("/")
    if not _BUCKET.fullmatch(name) or name.startswith("xn--") or name.endswith("-s3alias"):
        raise SetupError(f"{value!r} is not an S3 bucket for Redberry: s3://<name>, 3-63 lowercase letters, digits "
                         "and hyphens")
    return name


def is_bucket_url(value: str | None) -> bool:
    return bool(value) and value.strip().startswith("s3://") and bool(_BUCKET.fullmatch(
        value.strip().removeprefix("s3://").strip("/")))


def region(value: str | None, flag: str = "--region") -> str:
    if not _REGION.fullmatch(value or ""):
        raise SetupError(f"{flag}: an AWS region, e.g. eu-west-1 or us-east-1")
    return value  # type: ignore[return-value]


def retention(value: str) -> tuple[str, int]:
    """``7y`` or ``30d`` as Object Lock's ("Years", 7) or ("Days", 30)."""
    m = re.fullmatch(r"([1-9][0-9]*)([dy])", value or "")
    if not m:
        if re.fullmatch(r"[1-9][0-9]*[smh]", value or ""):
            raise SetupError(f"--retention {value}: Object Lock's shortest retention is one day; use e.g. 1d for a "
                             "test bucket, 7y for production")
        raise SetupError("--retention: a number and a unit, d or y (e.g. 7y or 30d)")
    n, unit = int(m.group(1)), m.group(2)
    days = n if unit == "d" else n * 365
    if days > MAX_RETENTION_DAYS:
        raise SetupError(f"--retention {value}: at most 100 years")
    return ("Days" if unit == "d" else "Years"), n


def _name(value: str, limit: int) -> str:
    return re.sub(r"[^A-Za-z0-9+=,.@_-]", "-", value)[:limit].strip("-")


def role_name_for(project: str) -> str:
    """IAM role names: up to 64 of letters, digits and ``+=,.@_-``."""
    return _name(f"redberry-vercel-{project}", 64)


def _oidc_host(team: str) -> str:
    return f"oidc.vercel.com/{team}"


def _json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=False)


def write_policy(buckets: list[str], prefix: str = RUNS_PREFIX) -> dict:
    """``s3:PutObject`` and ``s3:GetObject`` (the conflict read-back) on ``<bucket>/redberry/*``, and
    ``s3:ListBucket`` there (a missing object then reads as 404, not 403); no delete, no retention change,
    no governance bypass."""
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "RedberryStoredRuns", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": [f"arn:aws:s3:::{b}/{prefix}*" for b in buckets]},
        {"Sid": "RedberryStoredRunsList", "Effect": "Allow", "Action": "s3:ListBucket",
         "Resource": [f"arn:aws:s3:::{b}" for b in buckets],
         "Condition": {"StringLike": {"s3:prefix": [f"{prefix}*"]}}}]}


def sink_policy(buckets: dict[str, str]) -> dict:
    """The VALP sink: in the evidence bucket (Object Lock) create and read only; in the payloads and index
    buckets create, read, replace and delete (erasure); list in all three."""
    locked = [b for role, b in buckets.items() if role in LOCKED_ROLES]
    free = [b for role, b in buckets.items() if role not in LOCKED_ROLES]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ValpSinkEvidence", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": [f"arn:aws:s3:::{b}/*" for b in locked]},
        {"Sid": "ValpSinkPayloadsAndIndex", "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
         "Resource": [f"arn:aws:s3:::{b}/*" for b in free]},
        {"Sid": "ValpSinkList", "Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": [f"arn:aws:s3:::{b}" for b in buckets.values()]}]}


def create_only_policy(bucket: str) -> dict:
    """The bucket policy of an Object Lock bucket. Object Lock protects versions, not keys: S3 still takes a
    PutObject over an existing key (a new current version). So every PutObject must be a conditional
    create (``If-None-Match: *``), whoever sends it."""
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "CreateOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:PutObject",
        "Resource": f"arn:aws:s3:::{bucket}/*", "Condition": {"Null": {"s3:if-none-match": "true"}}}]}


def vercel_trust(account: str, team: str, project: str) -> dict:
    host = _oidc_host(team)
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Federated": f"arn:aws:iam::{account}:oidc-provider/{host}"},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {"StringEquals": {f"{host}:aud": oidc_audience(team),
                                       f"{host}:sub": oidc_subject(team, project)}}}]}


EC2_TRUST = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


@dataclass
class Plan:
    """What setup-aws makes; the steps turn it into aws commands."""

    region: str
    buckets: dict[str, str]                   # role -> bucket name ("runs" for a Vercel / Lambda store)
    retention: tuple[str, int]
    lock: bool = False
    target: str = "vercel"                    # vercel | function | evidence
    vercel_team: str | None = None
    vercel_project: str | None = None
    function: str | None = None
    function_region: str | None = None
    role: str | None = None
    profile: str | None = None
    locked: tuple[str, ...] = field(default=())

    @property
    def mode(self) -> str:
        return "COMPLIANCE" if self.lock else "GOVERNANCE"

    def _p(self) -> list[str]:
        return ["--profile", self.profile] if self.profile else []

    def url(self, role: str) -> str:
        return f"s3://{self.buckets[role]}"

    def retention_text(self) -> str:
        unit, n = self.retention
        return f"{n} {unit.lower() if n != 1 else unit.lower().rstrip('s')}"

    # ── the bucket(s) ──
    def create_bucket(self, role: str) -> list[str]:
        b = self.buckets[role]
        cmd = ["aws", "s3api", "create-bucket", "--bucket", b, "--region", self.region,
               "--object-ownership", "BucketOwnerEnforced"]
        if self.region != "us-east-1":
            cmd += ["--create-bucket-configuration", f"LocationConstraint={self.region}"]
        if role in self.locked:
            cmd.append("--object-lock-enabled-for-bucket")
        return [*cmd, *self._p()]

    def bucket_steps(self, role: str) -> list[tuple[str, list[str]]]:
        b, r = self.buckets[role], ["--region", self.region, *self._p()]
        steps = [
            (f"block all public access to {b}",
             ["aws", "s3api", "put-public-access-block", "--bucket", b, "--public-access-block-configuration",
              "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true", *r]),
            (f"encrypt {b} at rest by default (SSE-S3)",
             ["aws", "s3api", "put-bucket-encryption", "--bucket", b, "--server-side-encryption-configuration",
              _json({"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                                "BucketKeyEnabled": True}]}), *r]),
            (f"enforce bucket-owner object ownership on {b} (no ACLs)",
             ["aws", "s3api", "put-bucket-ownership-controls", "--bucket", b, "--ownership-controls",
              _json({"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}), *r]),
        ]
        if role in self.locked:
            steps.append((f"keep versioning on {b} (Object Lock needs it)",
                          ["aws", "s3api", "put-bucket-versioning", "--bucket", b, "--versioning-configuration",
                           "Status=Enabled", *r]))
            steps.append((f"accept only create-only writes (If-None-Match: *) in {b}: no overwrite of a key",
                          ["aws", "s3api", "put-bucket-policy", "--bucket", b, "--policy",
                           _json(create_only_policy(b)), *r]))
        return steps

    def lock_command(self, role: str) -> list[str]:
        unit, n = self.retention
        return ["aws", "s3api", "put-object-lock-configuration", "--bucket", self.buckets[role],
                "--object-lock-configuration",
                _json({"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": self.mode, unit: n}}}),
                "--region", self.region, *self._p()]

    def get_lock_command(self, role: str) -> list[str]:
        return ["aws", "s3api", "get-object-lock-configuration", "--bucket", self.buckets[role], "--region",
                self.region, "--output", "json", *self._p()]

    # ── who writes ──
    def policy_document(self) -> dict:
        if self.target == "evidence":
            return sink_policy({r: self.buckets[r] for r in EVIDENCE_ROLES})
        return write_policy([self.buckets["runs"]])

    def put_policy(self, role_name: str) -> list[str]:
        name = SINK_POLICY_NAME if self.target == "evidence" else POLICY_NAME
        return ["aws", "iam", "put-role-policy", "--role-name", role_name, "--policy-name", name,
                "--policy-document", _json(self.policy_document()), *self._p()]

    # The URL-valued arguments go in --cli-input-json: AWS CLI v1 fetches any parameter value that starts with
    # https:// and passes the page instead (cli_follow_urlparam), so --client-id-list https://vercel.com/<team>
    # failed with a 404 in the live test. A JSON document is never fetched, in v1 or v2.
    def create_oidc_provider(self) -> list[str]:
        doc = {"Url": f"https://{_oidc_host(self.vercel_team)}", "ClientIDList": [oidc_audience(self.vercel_team)]}
        return ["aws", "iam", "create-open-id-connect-provider", "--cli-input-json", _json(doc), *self._p()]

    def add_audience(self, arn: str) -> list[str]:
        doc = {"OpenIDConnectProviderArn": arn, "ClientID": oidc_audience(self.vercel_team)}
        return ["aws", "iam", "add-client-id-to-open-id-connect-provider", "--cli-input-json", _json(doc), *self._p()]

    def trust(self, account: str) -> dict:
        if self.target == "evidence":
            return EC2_TRUST
        return vercel_trust(account, self.vercel_team, self.vercel_project)

    def create_role(self, account: str) -> list[str]:
        why = ("Redberry: the bundle's VALP sink writes evidence" if self.target == "evidence" else
               f"Redberry: {self.vercel_team}/{self.vercel_project} (production) writes stored runs")
        return ["aws", "iam", "create-role", "--role-name", self.role, "--assume-role-policy-document",
                _json(self.trust(account)), "--description", why, *self._p()]

    def update_trust(self, account: str) -> list[str]:
        return ["aws", "iam", "update-assume-role-policy", "--role-name", self.role, "--policy-document",
                _json(self.trust(account)), *self._p()]

    def get_function(self) -> list[str]:
        return ["aws", "lambda", "get-function-configuration", "--function-name", self.function, "--region",
                self.function_region or self.region, "--output", "json", *self._p()]

    def instance_profile_steps(self) -> list[tuple[str, list[str]]]:
        return [(f"the instance profile {SINK_ROLE} (what an EC2 instance carries the role in)",
                 ["aws", "iam", "create-instance-profile", "--instance-profile-name", SINK_ROLE, *self._p()]),
                (f"put the role {SINK_ROLE} in it",
                 ["aws", "iam", "add-role-to-instance-profile", "--instance-profile-name", SINK_ROLE, "--role-name",
                  SINK_ROLE, *self._p()])]

    def record(self, v: dict[str, str]) -> dict:
        unit, n = self.retention
        rec = {"account_id": v["account"], "region": self.region, "target": self.target,
               "buckets": {role: {"name": name, "url": self.url(role)} for role, name in self.buckets.items()},
               "retention": {"mode": self.mode, unit.lower(): n}, "object_lock": list(self.locked),
               "locked": self.lock}
        if "runs" in self.buckets:
            rec["bucket"], rec["bucket_url"] = self.buckets["runs"], self.url("runs")
        if self.target == "vercel":
            rec.update({"vercel_team": self.vercel_team, "vercel_project": self.vercel_project,
                        "role_arn": v["role_arn"], "oidc_provider_arn": v["provider_arn"],
                        "oidc_audience": oidc_audience(self.vercel_team),
                        "oidc_subject": oidc_subject(self.vercel_team, self.vercel_project)})
        elif self.target == "function":
            rec["function"] = {"name": self.function, "region": self.function_region or self.region,
                               "role_arn": v["role_arn"], "policy": POLICY_NAME}
        else:
            rec.update({"role_arn": v["role_arn"], "instance_profile": SINK_ROLE})
        return rec

    def next_step(self) -> str:
        if self.target in ("vercel", "function"):
            return f"the bucket {self.url('runs')} is ready for its writer"
        return (f"the buckets {self.url('evidence')}, {self.url('payloads')} and {self.url('index')} are ready "
                f"(region {self.region})")


# ── running ─────────────────────────────────────────────────────────────────────────────────

#: How long the IAM steps wait for something just created (IAM is eventually consistent: a role naming a new
#: OIDC provider, or an instance profile naming a new role, can be refused for a few seconds).
RETRY_DELAYS_S = (3, 5, 5, 10, 10, 15)


def _err(r) -> str:
    return (getattr(r, "stderr", "") or "").strip()


def _not_yet_visible(cmd: list[str], r) -> bool:
    err = _err(r)
    if cmd[1] != "iam":
        return False
    return ("Invalid principal" in err or "MalformedPolicyDocument" in err and "principal" in err.lower()
            or "NoSuchEntity" in err and cmd[2] in ("add-role-to-instance-profile", "put-role-policy"))


def _already_exists(r) -> bool:
    err = _err(r)
    return any(k in err for k in ("EntityAlreadyExists", "BucketAlreadyOwnedByYou", "already exists"))


def _quota_one_role(cmd: list[str], r) -> bool:
    """add-role-to-instance-profile when the profile already holds a role (it holds at most one)."""
    return cmd[2:3] == ["add-role-to-instance-profile"] and "LimitExceeded" in _err(r)


def _run_step(why: str, cmd: list[str], run, out, sleep):
    out(f"{why} ...")
    r = aws(run, cmd)
    for delay in RETRY_DELAYS_S:
        if r.returncode == 0 or not _not_yet_visible(cmd, r):
            break
        out(f"  not visible to IAM yet; retrying in {delay}s")
        sleep(delay)
        r = aws(run, cmd)
    if r.returncode != 0:
        if _already_exists(r) or _quota_one_role(cmd, r):
            out("  already there")
            return r
        if "BucketAlreadyExists" in _err(r):
            raise SetupError(f"the bucket name {cmd[cmd.index('--bucket') + 1]} is taken (S3 bucket names are "
                             "global): pick another --bucket / --prefix")
        raise SetupError(f"{' '.join(cmd[:3])} failed: {_err(r)}")
    return r


def _parse(r) -> dict:
    try:
        value = json.loads(getattr(r, "stdout", "") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _sh(arg: str) -> str:
    """Shell-quote ``arg``, leaving ``${VAR}`` to expand."""
    parts = re.split(r"(\$\{[A-Z_]+\})", arg)
    if len(parts) == 1:
        return shlex.quote(arg)
    return "".join(f'"{p}"' if re.fullmatch(r"\$\{[A-Z_]+\}", p) else shlex.quote(p) if p else "" for p in parts)


def _line(cmd: list[str]) -> str:
    return " ".join(_sh(c) for c in cmd)


def _read_vercel_project(repo: Path) -> str | None:
    try:
        return json.loads((repo / ".vercel" / "project.json").read_text(encoding="utf-8")).get("projectName")
    except (OSError, ValueError):
        return None


def plan_from_args(a) -> Plan:
    targets = [t for t, on in (("vercel", bool(a.vercel_team)), ("function", bool(a.function)),
                               ("evidence", bool(a.evidence))) if on]
    if len(targets) != 1:
        raise SetupError("say what writes to the bucket, one of: --vercel-team <team> (a Vercel project), "
                         "--function <name> (a Lambda function) or --evidence (the bundle's VALP sink)")
    target = targets[0]
    reg = region(a.region)
    if target == "evidence":
        if a.bucket:
            raise SetupError("--evidence makes three buckets, <prefix>-evidence, <prefix>-payloads and <prefix>-index: "
                             "drop --bucket and pass --prefix")
        if not a.prefix:
            raise SetupError("--evidence needs --prefix <name> (S3 bucket names are global; e.g. acme-valp)")
        buckets = {role: bucket_name(f"{a.prefix}-{role}") for role in EVIDENCE_ROLES}
        locked = LOCKED_ROLES
    else:
        if a.prefix:
            raise SetupError("--prefix is for --evidence")
        if not a.bucket:
            raise SetupError("--bucket s3://<name>: the bucket to create (S3 bucket names are global)")
        buckets = {"runs": bucket_name(a.bucket)}
        locked = ("runs",)
    keep = retention(a.retention or DEFAULT_RETENTION)
    vercel_project, role = None, None
    if target == "vercel":
        vercel_project = a.vercel_project or _read_vercel_project(Path(a.repo or ".").resolve())
        if not vercel_project:
            raise SetupError("name the Vercel project: --vercel-project <name> (no .vercel/project.json here)")
        role = a.role or role_name_for(vercel_project)
    elif target == "evidence":
        role = a.role or SINK_ROLE
    elif a.role:
        raise SetupError("--role is for --vercel-team or --evidence (--function uses the function's execution role)")
    fn_region = region(a.function_region, "--function-region") if getattr(a, "function_region", None) else None
    return Plan(region=reg, buckets=buckets, retention=keep, lock=a.lock, target=target, vercel_team=a.vercel_team,
                vercel_project=vercel_project, function=a.function, function_region=fn_region, role=role,
                profile=a.profile, locked=locked)


LOCK_WARNING = ("WARNING: --lock sets COMPLIANCE mode. Every object written to {buckets} is then kept for {keep}: "
                "nobody, not even the AWS account's root user, can delete it, overwrite it or shorten its "
                "retention, and the bucket cannot be deleted while it holds one. THIS CANNOT BE UNDONE. Without "
                "--lock the bucket uses GOVERNANCE mode, fine for a test.")


def _print(plan: Plan, out) -> int:
    p = plan._p()
    out("# redberry setup-aws: run these (bash), signed in to the AWS CLI (aws configure / aws sso login).")
    if plan.lock:
        for line in LOCK_WARNING.format(buckets=", ".join(plan.url(r) for r in plan.locked),
                                        keep=plan.retention_text()).split(". "):
            out(f"# {line.rstrip('.')}.")
    out(f"ACCOUNT_ID=$({_line(['aws', 'sts', 'get-caller-identity', '--query', 'Account', '--output', 'text', *p])})")
    for role in plan.buckets:
        out(f"# the bucket {plan.buckets[role]}" + (" (Object Lock, so versioning too)" if role in plan.locked else ""))
        out(_line(plan.create_bucket(role)))
        for why, cmd in plan.bucket_steps(role):
            out(f"# {why}")
            out(_line(cmd))
        if role in plan.locked:
            out(f"# keep every new object in {plan.buckets[role]} for {plan.retention_text()} ({plan.mode} mode)")
            out(_line(plan.lock_command(role)))
    if plan.target == "vercel":
        host = _oidc_host(plan.vercel_team)
        out(f"# Vercel's OIDC issuer for the team {plan.vercel_team} (skip if `aws iam list-open-id-connect-providers` "
            "already lists it; then add the audience with add-client-id-to-open-id-connect-provider)")
        out(_line(plan.create_oidc_provider()))
        out(f"# the role only {oidc_subject(plan.vercel_team, plan.vercel_project)} can assume")
        out(_line(plan.create_role("${ACCOUNT_ID}")))
        out(f"# it may write {RUNS_PREFIX}* in the bucket, and nothing else")
        out(_line(plan.put_policy(plan.role)))
        out(f"ROLE_ARN=arn:aws:iam::$ACCOUNT_ID:role/{plan.role}   # (issuer: https://{host})")
    elif plan.target == "function":
        out(f"# the function's execution role: may write {RUNS_PREFIX}* in the bucket")
        out(f"ROLE_NAME=$({_line(plan.get_function())} --query Role --output text | sed 's#.*/##')")
        out(_line(plan.put_policy("${ROLE_NAME}")))
    else:
        out("# the sink's role, trusted by EC2, and its instance profile")
        out(_line(plan.create_role("${ACCOUNT_ID}")))
        out(_line(plan.put_policy(plan.role)))
        for why, cmd in plan.instance_profile_steps():
            out(f"# {why}")
            out(_line(cmd))
        _evidence_notes(plan, lambda m: out(f"# {m}"))
    out(f"# then: {plan.next_step()}")
    return 0


def _evidence_notes(plan: Plan, out) -> None:
    out(f"Attach it to the EC2 instance the bundle runs on:  aws ec2 associate-iam-instance-profile --instance-id "
        f"<i-…> --iam-instance-profile Name={SINK_ROLE} --region <the instance's region>")
    out("The sink runs in a container: let it reach the instance's credentials (IMDSv2) with a hop limit of 2:  "
        "aws ec2 modify-instance-metadata-options --instance-id <i-…> --http-put-response-hop-limit 2 "
        "--http-endpoint enabled")
    out(f"Off AWS: make an IAM user with the policy {SINK_POLICY_NAME} (aws iam get-role-policy --role-name {SINK_ROLE} "
        "--policy-name " + SINK_POLICY_NAME + ") and put its AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in the "
        "bundle's .env (or AWS_PROFILE with ~/.aws).")


def _confirm_lock(a, plan: Plan, out) -> None:
    warning = LOCK_WARNING.format(buckets=", ".join(plan.url(r) for r in plan.locked), keep=plan.retention_text())
    out(warning)
    if getattr(a, "yes", False):
        return
    if not sys.stdin.isatty():
        raise SetupError("--lock (COMPLIANCE, irreversible) needs --yes when there is no terminal to ask")
    if input("Type COMPLIANCE to lock it: ").strip() != "COMPLIANCE":
        raise SetupError("not locked; nothing was changed")


def _current_mode(plan: Plan, role: str, run) -> str | None:
    r = aws(run, plan.get_lock_command(role))
    rule = ((_parse(r).get("ObjectLockConfiguration") or {}).get("Rule") or {}).get("DefaultRetention") or {}
    return rule.get("Mode") if r.returncode == 0 else None


def cmd_setup_aws(a, out=print, run=None, sleep=None) -> int:
    run = run or subprocess.run
    sleep = sleep or time.sleep
    plan = plan_from_args(a)
    repo = Path(getattr(a, "repo", ".") or ".").resolve()
    if a.print:
        return _print(plan, out)
    if plan.lock:
        _confirm_lock(a, plan, out)

    r = aws(run, ["aws", "sts", "get-caller-identity", "--output", "json", *plan._p()])
    who = _parse(r)
    if r.returncode != 0 or not who.get("Account"):
        raise SetupError(f"the AWS CLI is not signed in: run `aws configure` or `aws sso login` ({_err(r)})")
    v = {"account": str(who["Account"])}
    out(f"AWS account {v['account']} as {who.get('Arn', '?')}")

    for role in plan.buckets:
        b = plan.buckets[role]
        head = aws(run, ["aws", "s3api", "head-bucket", "--bucket", b, "--region", plan.region, *plan._p()])
        if head.returncode == 0:
            out(f"the bucket {b} is there")
            if role in plan.locked:
                lock = aws(run, plan.get_lock_command(role))
                if lock.returncode != 0 or (_parse(lock).get("ObjectLockConfiguration") or {}).get(
                        "ObjectLockEnabled") != "Enabled":
                    out(f"  {b} has no Object Lock: turning it on (versioning first)")
        else:
            _run_step(f"create the bucket {b}" + (" with Object Lock" if role in plan.locked else ""),
                      plan.create_bucket(role), run, out, sleep)
        for why, cmd in plan.bucket_steps(role):
            _run_step(why, cmd, run, out, sleep)
        if role in plan.locked:
            if not plan.lock and _current_mode(plan, role, run) == "COMPLIANCE":
                out(f"  {b} already keeps objects in COMPLIANCE mode: left as it is")
            else:
                _run_step(f"keep every new object in {b} for {plan.retention_text()} ({plan.mode} mode)",
                          plan.lock_command(role), run, out, sleep)

    if plan.target == "vercel":
        host = _oidc_host(plan.vercel_team)
        listed = _parse(aws(run, ["aws", "iam", "list-open-id-connect-providers", "--output", "json", *plan._p()]))
        arn = next((p.get("Arn") for p in listed.get("OpenIDConnectProviderList") or []
                    if str(p.get("Arn", "")).endswith(f":oidc-provider/{host}")), None)
        if arn:
            _run_step(f"Vercel's issuer {host} is there; make sure it accepts the audience {oidc_audience(plan.vercel_team)}",
                      plan.add_audience(arn), run, out, sleep)
        else:
            made = _run_step(f"trust Vercel's OIDC issuer https://{host}", plan.create_oidc_provider(), run, out, sleep)
            arn = _parse(made).get("OpenIDConnectProviderArn") or f"arn:aws:iam::{v['account']}:oidc-provider/{host}"
        v["provider_arn"] = arn
        v["role_arn"] = _role(plan, v, run, out, sleep)
    elif plan.target == "function":
        r = aws(run, plan.get_function())
        fn = _parse(r)
        if r.returncode != 0 or not fn.get("Role"):
            raise SetupError(f"cannot read the Lambda function {plan.function} (aws lambda get-function-configuration): "
                             f"{_err(r) or 'no Role'}")
        v["role_arn"] = fn["Role"]
        role_name = fn["Role"].rsplit("/", 1)[-1]
        _run_step(f"let the function's execution role {role_name} write {RUNS_PREFIX}* in {plan.buckets['runs']}",
                  plan.put_policy(role_name), run, out, sleep)
    else:
        v["role_arn"] = _role(plan, v, run, out, sleep)
        for why, cmd in plan.instance_profile_steps():
            _run_step(why, cmd, run, out, sleep)

    record = plan.record(v)
    (repo / RECORD).parent.mkdir(parents=True, exist_ok=True)
    (repo / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    out(f"Ready: {', '.join(plan.url(r) for r in plan.buckets)} ({plan.region}).")
    if plan.target == "vercel":
        out(f"Only {oidc_subject(plan.vercel_team, plan.vercel_project)} can assume {v['role_arn']}, and it may only "
            f"write {RUNS_PREFIX}* there.")
    elif plan.target == "function":
        out(f"{plan.function}'s execution role may now write {RUNS_PREFIX}* there ({POLICY_NAME}).")
    else:
        _evidence_notes(plan, out)
    for role in plan.locked:
        if plan.lock:
            out(f"Objects in {plan.buckets[role]} are kept for {plan.retention_text()} in COMPLIANCE mode (irreversible).")
        else:
            out(f"Objects in {plan.buckets[role]} are kept for {plan.retention_text()} in GOVERNANCE mode. Once the "
                "setup is proven, make it COMPLIANCE (IRREVERSIBLE) with  redberry setup-aws … --lock")
    out(f"Recorded in {RECORD}. Next: {plan.next_step()}")
    return 0


def _role(plan: Plan, v: dict[str, str], run, out, sleep) -> str:
    made = _run_step(f"the role {plan.role}", plan.create_role(v["account"]), run, out, sleep)
    if made.returncode != 0:                      # there already: make sure it trusts what this setup says
        _run_step(f"update who may assume {plan.role}", plan.update_trust(v["account"]), run, out, sleep)
    arn = ((_parse(made).get("Role") or {}).get("Arn")) or f"arn:aws:iam::{v['account']}:role/{plan.role}"
    _run_step(f"let {plan.role} write to " + ", ".join(plan.buckets.values()) + ", and nothing else",
              plan.put_policy(plan.role), run, out, sleep)
    return arn


# ── what connect reads ──────────────────────────────────────────────────────────────────────


def load_record(*dirs: Path) -> dict:
    """The first ``.redberry/aws.json`` in ``dirs``, or {}."""
    for d in dirs:
        try:
            return json.loads((d / RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def record_for(record: dict, bucket: str) -> bool:
    """Whether setup-aws's ``record`` made the bucket ``bucket`` (a name)."""
    return any((b or {}).get("name") == bucket for b in (record.get("buckets") or {}).values())


__all__ = ["RECORD", "Plan", "SetupError", "aws", "aws_binary", "bucket_name", "cmd_setup_aws", "create_only_policy",
           "is_bucket_url",
           "load_record", "plan_from_args", "record_for", "region", "retention", "role_name_for", "sink_policy",
           "write_policy"]
