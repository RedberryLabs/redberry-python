"""`redberry setup-aws` and `redberry connect --platform aws-lambda`, against a faked `aws` (as
test_cli_azure.py does for `az`). No AWS resource is created or changed, and no Redberry API is called:
connect only wires the function's code (``init()`` finds the installation from the function's own credentials).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from redberry_cli import aws_lambda as awl
from redberry_cli import aws_setup
from redberry_cli import main as cli

ACCOUNT, REGION = "123456789012", "eu-west-1"
BUCKET = "acme-redberry-runs"
FN_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:refund-agent"
EXEC_ROLE = f"arn:aws:iam::{ACCOUNT}:role/service-role/refund-agent-role-x1"
PROVIDER_ARN = f"arn:aws:iam::{ACCOUNT}:oidc-provider/oidc.vercel.com/acme"


@pytest.fixture(autouse=True)
def _aws_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS", "aws")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(k, raising=False)


class _Aws:
    """The faked ``aws``. ``fail``: a substring of a command that fails; ``exists``: commands that answer
    "already exists"; ``buckets``: buckets head-bucket finds; ``unseen``: how many times create-role fails first
    because IAM does not see the new OIDC provider yet."""

    def __init__(self, fail=None, exists=(), buckets=(), providers=(), unseen=0, function=None, lock_mode=None,
                 tags=None):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.fail, self.exists, self.buckets = fail, set(exists), set(buckets)
        self.providers, self.unseen, self.lock_mode, self.tags = list(providers), unseen, lock_mode, tags or {}
        self.function = function or {}

    def __call__(self, cmd, cwd=None, input=None, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        r = type("R", (), {})()
        r.returncode, r.stdout, r.stderr = 0, "", ""
        line = " ".join(cmd)
        sub = cmd[1:3]
        if sub == ["sts", "get-caller-identity"]:
            r.stdout = json.dumps({"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/jordan"})
        elif sub == ["s3api", "head-bucket"]:
            if cmd[cmd.index("--bucket") + 1] not in self.buckets:
                r.returncode, r.stderr = 254, "An error occurred (404) when calling the HeadBucket operation: Not Found"
        elif sub == ["s3api", "get-object-lock-configuration"]:
            if self.lock_mode:
                r.stdout = json.dumps({"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled", "Rule": {
                    "DefaultRetention": {"Mode": self.lock_mode, "Years": 7}}}})
            else:
                r.returncode, r.stderr = 254, "ObjectLockConfigurationNotFoundError"
        elif sub == ["iam", "list-open-id-connect-providers"]:
            r.stdout = json.dumps({"OpenIDConnectProviderList": [{"Arn": a} for a in self.providers]})
        elif sub == ["iam", "create-open-id-connect-provider"]:
            r.stdout = json.dumps({"OpenIDConnectProviderArn": PROVIDER_ARN})
        elif sub == ["iam", "create-role"]:
            if self.unseen > 0:
                self.unseen -= 1
                r.returncode, r.stderr = 254, "An error occurred (MalformedPolicyDocument): Invalid principal in policy"
            else:
                name = cmd[cmd.index("--role-name") + 1]
                r.stdout = json.dumps({"Role": {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}"}})
        elif sub == ["lambda", "get-function-configuration"]:
            r.stdout = json.dumps(self.function)
        elif sub == ["lambda", "list-tags"]:
            r.stdout = json.dumps({"Tags": self.tags})
        elif sub == ["lambda", "update-function-configuration"]:
            path = cmd[cmd.index("--environment") + 1]
            assert path.startswith("file://")
            f = Path(path.removeprefix("file://"))
            self.envs.append({"mode": stat.S_IMODE(f.stat().st_mode), "doc": json.loads(f.read_text()),
                              "revision": cmd[cmd.index("--revision-id") + 1] if "--revision-id" in cmd else None})
            r.stdout = "InProgress\n"
        elif sub == ["configure", "get"]:
            r.stdout = REGION + "\n"
        if self.fail and self.fail in line:
            r.returncode, r.stderr = 254, "An error occurred (AccessDenied): boom"
        if any(e in line for e in self.exists):
            r.returncode, r.stderr = 254, "An error occurred (EntityAlreadyExists): Role with name x already exists."
        return r

    def kinds(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls]

    def first(self, *words) -> list[str]:
        return next(c for c in self.calls if c[1:1 + len(words)] == list(words))




# ── setup-aws ───────────────────────────────────────────────────────────────────────────────


def _setup(*argv, run=None, slept=None, region=REGION):
    args = cli.parser().parse_args(["setup-aws", "--region", region, *argv])
    lines: list[str] = []
    rc = aws_setup.cmd_setup_aws(args, out=lines.append, run=run,
                                 sleep=(slept.append if slept is not None else lambda s: None))
    return rc, "\n".join(lines)


def _arg_json(cmd: list[str], flag: str) -> dict:
    return json.loads(cmd[cmd.index(flag) + 1])


def test_setup_aws_prints_the_commands_for_a_vercel_project(tmp_path):
    rc, out = _setup("--bucket", f"s3://{BUCKET}", "--vercel-team", "acme", "--vercel-project", "acme-support",
                     "--repo", str(tmp_path), "--print")
    assert rc == 0
    assert (f"aws s3api create-bucket --bucket {BUCKET} --region {REGION} --object-ownership BucketOwnerEnforced "
            f"--create-bucket-configuration LocationConstraint={REGION} --object-lock-enabled-for-bucket") in out
    assert ("--public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,"
            "RestrictPublicBuckets=true") in out
    assert '"SSEAlgorithm":"AES256"' in out and "put-bucket-versioning" in out
    assert ('\'{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"GOVERNANCE","Years":7}}}\'') in out
    # URL values travel as JSON: AWS CLI v1 would fetch a bare https:// argument (cli_follow_urlparam).
    assert ("aws iam create-open-id-connect-provider --cli-input-json "
            "'{\"Url\":\"https://oidc.vercel.com/acme\",\"ClientIDList\":[\"https://vercel.com/acme\"]}'") in out
    assert '"oidc.vercel.com/acme:sub":"owner:acme:project:acme-support:environment:production"' in out
    assert "\"Federated\":\"arn:aws:iam::'\"${ACCOUNT_ID}\"':oidc-provider/oidc.vercel.com/acme\"" in out
    assert f'"Resource":["arn:aws:s3:::{BUCKET}/redberry/*"]' in out
    assert f"# then: the bucket s3://{BUCKET} is ready for its writer" in out
    assert "COMPLIANCE" not in out and not (tmp_path / ".redberry").exists()


def test_setup_aws_runs_them_and_records_what_it_made(tmp_path):
    run, slept = _Aws(unseen=2), []
    rc, out = _setup("--bucket", BUCKET, "--vercel-team", "acme", "--vercel-project", "acme-support",
                     "--repo", str(tmp_path), run=run, slept=slept)
    assert rc == 0 and slept == [3, 5] and out.count("not visible to IAM yet") == 2
    assert run.kinds() == ["sts get-caller-identity", "s3api head-bucket", "s3api create-bucket",
                           "s3api put-public-access-block", "s3api put-bucket-encryption",
                           "s3api put-bucket-ownership-controls", "s3api put-bucket-versioning",
                           "s3api put-bucket-policy", "s3api get-object-lock-configuration", "s3api put-object-lock-configuration",
                           "iam list-open-id-connect-providers", "iam create-open-id-connect-provider",
                           "iam create-role", "iam create-role", "iam create-role", "iam put-role-policy"]
    assert all(c[0] == "aws" for c in run.calls)
    assert "--object-lock-enabled-for-bucket" in run.first("s3api", "create-bucket")
    lock = _arg_json(run.first("s3api", "put-object-lock-configuration"), "--object-lock-configuration")
    assert lock["Rule"]["DefaultRetention"] == {"Mode": "GOVERNANCE", "Years": 7}
    trust = _arg_json(run.first("iam", "create-role"), "--assume-role-policy-document")
    (st,) = trust["Statement"]
    assert st["Principal"] == {"Federated": PROVIDER_ARN} and st["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert st["Condition"]["StringEquals"] == {"oidc.vercel.com/acme:aud": "https://vercel.com/acme",
                                               "oidc.vercel.com/acme:sub": "owner:acme:project:acme-support:environment:production"}
    policy = run.first("iam", "put-role-policy")
    assert policy[policy.index("--role-name") + 1] == "redberry-vercel-acme-support"
    allow, listing = _arg_json(policy, "--policy-document")["Statement"]
    assert allow["Action"] == ["s3:PutObject", "s3:GetObject"] and allow["Resource"] == [f"arn:aws:s3:::{BUCKET}/redberry/*"]
    assert listing["Action"] == "s3:ListBucket" and listing["Resource"] == [f"arn:aws:s3:::{BUCKET}"]
    granted = json.dumps(_arg_json(policy, "--policy-document"))
    assert "Delete" not in granted and "Retention" not in granted and "Bypass" not in granted
    (deny,) = _arg_json(run.first("s3api", "put-bucket-policy"), "--policy")["Statement"]
    assert (deny["Effect"], deny["Principal"], deny["Action"]) == ("Deny", "*", "s3:PutObject")
    assert deny["Resource"] == f"arn:aws:s3:::{BUCKET}/*"
    assert deny["Condition"] == {"Null": {"s3:if-none-match": "true"}}          # creates only: no overwrite
    record = json.loads((tmp_path / ".redberry" / "aws.json").read_text())
    assert record["bucket_url"] == f"s3://{BUCKET}" and record["region"] == REGION and record["account_id"] == ACCOUNT
    assert record["role_arn"] == f"arn:aws:iam::{ACCOUNT}:role/redberry-vercel-acme-support"
    assert record["oidc_provider_arn"] == PROVIDER_ARN and record["retention"] == {"mode": "GOVERNANCE", "years": 7}
    assert record["locked"] is False and "secret" not in json.dumps(record).lower()
    assert f"Next: the bucket s3://{BUCKET} is ready for its writer" in out
    assert "GOVERNANCE mode. Once the setup is proven" in out


def test_setup_aws_again_changes_nothing_it_need_not(tmp_path):
    run = _Aws(buckets={BUCKET}, providers=[PROVIDER_ARN], exists=("iam create-role",))
    rc, out = _setup("--bucket", BUCKET, "--vercel-team", "acme", "--vercel-project", "acme-support",
                     "--repo", str(tmp_path), run=run)
    assert rc == 0 and "create-bucket" not in " ".join(run.kinds()) and "already there" in out
    assert "iam create-open-id-connect-provider" not in run.kinds()
    add = run.first("iam", "add-client-id-to-open-id-connect-provider")
    doc = json.loads(add[add.index("--cli-input-json") + 1])
    assert doc == {"OpenIDConnectProviderArn": PROVIDER_ARN, "ClientID": "https://vercel.com/acme"}
    assert "iam update-assume-role-policy" in run.kinds()          # the role was there: its trust is set again


def test_setup_aws_lock_is_compliance_and_asks_first(tmp_path, monkeypatch):
    monkeypatch.setattr(aws_setup.sys.stdin, "isatty", lambda: False)
    with pytest.raises(aws_setup.SetupError, match="needs --yes"):
        _setup("--bucket", BUCKET, "--function", "refund-agent", "--lock", "--repo", str(tmp_path), run=_Aws())
    run = _Aws(function={"Role": EXEC_ROLE})
    rc, out = _setup("--bucket", BUCKET, "--function", "refund-agent", "--lock", "--yes", "--retention", "10y",
                     "--repo", str(tmp_path), run=run)
    assert rc == 0 and "THIS CANNOT BE UNDONE" in out and "root user" in out
    lock = _arg_json(run.first("s3api", "put-object-lock-configuration"), "--object-lock-configuration")
    assert lock["Rule"]["DefaultRetention"] == {"Mode": "COMPLIANCE", "Years": 10}
    assert json.loads((tmp_path / ".redberry" / "aws.json").read_text())["locked"] is True
    _rc, printed = _setup("--bucket", BUCKET, "--function", "f", "--lock", "--print")
    assert "# WARNING: --lock sets COMPLIANCE mode" in printed and '"Mode":"COMPLIANCE"' in printed


def test_setup_aws_never_turns_compliance_back_into_governance(tmp_path):
    run = _Aws(buckets={BUCKET}, lock_mode="COMPLIANCE", function={"Role": EXEC_ROLE})
    rc, out = _setup("--bucket", BUCKET, "--function", "refund-agent", "--repo", str(tmp_path), run=run)
    assert rc == 0 and "already keeps objects in COMPLIANCE mode" in out
    assert "s3api put-object-lock-configuration" not in run.kinds()


def test_setup_aws_function_puts_the_policy_on_its_execution_role(tmp_path):
    run = _Aws(function={"FunctionArn": FN_ARN, "Role": EXEC_ROLE})
    rc, out = _setup("--bucket", BUCKET, "--function", "refund-agent", "--function-region", "us-east-2",
                     "--retention", "1d", "--repo", str(tmp_path), run=run)
    assert rc == 0
    get = run.first("lambda", "get-function-configuration")
    assert get[get.index("--region") + 1] == "us-east-2"
    policy = run.first("iam", "put-role-policy")
    assert policy[policy.index("--role-name") + 1] == "refund-agent-role-x1"          # the path is not the name
    assert not any(k.startswith("iam create") for k in run.kinds())
    lock = _arg_json(run.first("s3api", "put-object-lock-configuration"), "--object-lock-configuration")
    assert lock["Rule"]["DefaultRetention"] == {"Mode": "GOVERNANCE", "Days": 1}
    record = json.loads((tmp_path / ".redberry" / "aws.json").read_text())
    assert record["function"] == {"name": "refund-agent", "region": "us-east-2", "role_arn": EXEC_ROLE,
                                  "policy": "redberry-s3-write"}
    assert f"Next: the bucket s3://{BUCKET} is ready for its writer" in out


def test_setup_aws_us_east_1_has_no_location_constraint(tmp_path):
    _rc, out = _setup("--bucket", BUCKET, "--function", "f", "--print", region="us-east-1")
    assert "LocationConstraint" not in out


def test_setup_aws_evidence_makes_three_buckets_one_locked(tmp_path):
    run = _Aws()
    rc, out = _setup("--evidence", "--prefix", "acme-valp", "--repo", str(tmp_path), run=run)
    assert rc == 0
    made = [c[c.index("--bucket") + 1] for c in run.calls if c[1:3] == ["s3api", "create-bucket"]]
    assert made == ["acme-valp-evidence", "acme-valp-payloads", "acme-valp-index"]
    locked = [c[c.index("--bucket") + 1] for c in run.calls if "--object-lock-enabled-for-bucket" in c]
    assert locked == ["acme-valp-evidence"]
    versioned = [c[c.index("--bucket") + 1] for c in run.calls if c[1:3] == ["s3api", "put-bucket-versioning"]]
    assert versioned == ["acme-valp-evidence"]           # payloads and index: no versions, erasure really deletes
    policies = [c[c.index("--bucket") + 1] for c in run.calls if c[1:3] == ["s3api", "put-bucket-policy"]]
    assert policies == ["acme-valp-evidence"]            # create-only where Object Lock is
    assert sum(c[1:3] == ["s3api", "put-public-access-block"] for c in run.calls) == 3
    trust = _arg_json(run.first("iam", "create-role"), "--assume-role-policy-document")
    assert trust["Statement"][0]["Principal"] == {"Service": "ec2.amazonaws.com"}
    evidence, rest, listing = _arg_json(run.first("iam", "put-role-policy"), "--policy-document")["Statement"]
    assert evidence["Action"] == ["s3:PutObject", "s3:GetObject"]
    assert evidence["Resource"] == ["arn:aws:s3:::acme-valp-evidence/*"]
    assert "s3:DeleteObject" in rest["Action"]
    assert rest["Resource"] == ["arn:aws:s3:::acme-valp-payloads/*", "arn:aws:s3:::acme-valp-index/*"]
    assert listing["Action"][0] == "s3:ListBucket"
    assert listing["Resource"] == [f"arn:aws:s3:::acme-valp-{r}" for r in ("evidence", "payloads", "index")]
    assert "iam create-instance-profile" in run.kinds() and "iam add-role-to-instance-profile" in run.kinds()
    assert "associate-iam-instance-profile" in out and "--http-put-response-hop-limit 2" in out
    assert ("the buckets s3://acme-valp-evidence, s3://acme-valp-payloads and s3://acme-valp-index are ready "
            f"(region {REGION})") in out
    record = json.loads((tmp_path / ".redberry" / "aws.json").read_text())
    assert record["object_lock"] == ["evidence"] and record["instance_profile"] == "redberry-valp-sink"
    # Again: the profile already holds the role (IAM: LimitExceeded) is fine.
    run2 = _Aws(buckets={"acme-valp-evidence", "acme-valp-payloads", "acme-valp-index"},
                fail="add-role-to-instance-profile")
    run2.fail = None
    rc, _ = _setup("--evidence", "--prefix", "acme-valp", "--repo", str(tmp_path), run=run2)
    assert rc == 0


def test_setup_aws_checks_its_arguments(tmp_path):
    with pytest.raises(aws_setup.SetupError, match="one of: --vercel-team"):
        _setup("--bucket", BUCKET, "--print")
    with pytest.raises(aws_setup.SetupError, match="one of"):
        _setup("--bucket", BUCKET, "--evidence", "--function", "f", "--print")
    with pytest.raises(aws_setup.SetupError, match="--vercel-project"):
        _setup("--bucket", BUCKET, "--vercel-team", "acme", "--repo", str(tmp_path), "--print")
    with pytest.raises(aws_setup.SetupError, match="--prefix"):
        _setup("--evidence", "--print")
    with pytest.raises(aws_setup.SetupError, match="drop --bucket"):
        _setup("--evidence", "--bucket", BUCKET, "--print")
    with pytest.raises(aws_setup.SetupError, match="not an S3 bucket"):
        _setup("--bucket", "Acme_Bucket", "--function", "f", "--print")
    with pytest.raises(aws_setup.SetupError, match="--region"):
        _setup("--bucket", BUCKET, "--function", "f", "--print", region="europe")
    with pytest.raises(aws_setup.SetupError, match="shortest retention is one day"):
        _setup("--bucket", BUCKET, "--function", "f", "--retention", "12h", "--print")
    assert aws_setup.retention("30d") == ("Days", 30) and aws_setup.retention("7y") == ("Years", 7)


def test_setup_aws_stops_on_a_real_failure(tmp_path):
    with pytest.raises(aws_setup.SetupError, match="put-public-access-block failed: An error occurred \\(AccessDenied"):
        _setup("--bucket", BUCKET, "--function", "f", "--repo", str(tmp_path), run=_Aws(fail="put-public-access-block"))
    assert not (tmp_path / ".redberry" / "aws.json").exists()
    taken = _Aws(fail="create-bucket")
    taken.fail = None

    def run(cmd, **kw):
        r = taken(cmd, **kw)
        if cmd[1:3] == ["s3api", "create-bucket"]:
            r.returncode, r.stderr = 254, "An error occurred (BucketAlreadyExists) when calling the CreateBucket"
        return r

    with pytest.raises(aws_setup.SetupError, match="is taken"):
        _setup("--bucket", BUCKET, "--function", "f", "--repo", str(tmp_path), run=run)
    with pytest.raises(aws_setup.SetupError, match="not signed in"):
        _setup("--bucket", BUCKET, "--function", "f", "--repo", str(tmp_path), run=_Aws(fail="get-caller-identity"))


def test_aws_binary_falls_back_to_the_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS")
    monkeypatch.setattr(aws_setup.shutil, "which", lambda _n: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert aws_setup.aws_binary() == "aws"
    venv = tmp_path / ".aws-cli-venv" / "bin" / "aws"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    assert aws_setup.aws_binary() == str(venv)
    monkeypatch.setattr(aws_setup.shutil, "which", lambda _n: "/usr/local/bin/aws")
    assert aws_setup.aws_binary() == "/usr/local/bin/aws"
    monkeypatch.setenv("AWS", "/opt/aws")
    assert aws_setup.aws_binary() == "/opt/aws"


# ── connect --platform aws-lambda: Node.js ──────────────────────────────────────────────────

INDEX_MJS = """\
import { generateText } from 'ai';
import { lookup } from './lib/orders.mjs';

export const handler = async (event, context) => {
  const order = await lookup(event.orderId);
  const { text } = await generateText({
    model: 'openai/gpt-4o-mini',
    prompt: `Refund ${order.id}?`,
    temperature: 0.3,
  });
  return { statusCode: 200, body: JSON.stringify({ text }) };
};
"""

ORDERS_MJS = """\
export async function lookup(id) {
  return { id };
}
"""

SECRET_ENV = {"OPENAI_API_KEY": "sk-live-very-secret", "TABLE": "orders"}


def _function(handler="index.handler", runtime="nodejs22.x", env=None, **extra):
    return {"FunctionName": "refund-agent", "FunctionArn": FN_ARN, "Runtime": runtime, "Handler": handler,
            "Role": EXEC_ROLE, "Timeout": 60, "PackageType": "Zip", "RevisionId": "rev-1",
            "Environment": {"Variables": dict(SECRET_ENV if env is None else env)}, "Architectures": ["arm64"],
            **extra}


@pytest.fixture
def node_fn(tmp_path):
    repo = tmp_path / "refund"
    (repo / "lib").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({"name": "refund", "type": "module",
                                                   "dependencies": {"ai": "^7.0.148"}}, indent=2) + "\n")
    (repo / "index.mjs").write_text(INDEX_MJS)
    (repo / "lib" / "orders.mjs").write_text(ORDERS_MJS)
    return repo


def _lambda_connect(api, repo, *argv, run=None, ask=None):
    args = cli.parser().parse_args(["--api", api, "connect", "--platform", "aws-lambda", "--repo", str(repo),
                                    "--function", "refund-agent", "--region", REGION, *argv])
    lines: list[str] = []
    if ask is None:
        rc = cli.cmd_connect(args, out=lines.append, run=run)
    else:
        rc = awl.cmd_connect(args, out=lines.append, run=run, ask=ask)
    return rc, "\n".join(lines)


@pytest.fixture
def api():
    """No Redberry API is called on this path: an address nothing listens on."""
    return "http://127.0.0.1:9"


def test_connect_node_lambda(api, node_fn):
    run = _Aws(function=_function())
    rc, out = _lambda_connect(api, node_fn, "--yes", run=run)
    assert rc == 0
    index = (node_fn / "index.mjs").read_text()
    assert index.startswith("// Redberry (redberry connect --platform aws-lambda)")
    assert "import { init } from '@redberrylabs/otel/init';" in index
    assert 'const redberry = init({ agent: "refund-agent" });' in index
    assert "registerTelemetry(new OpenTelemetry({ runtimeContext: true }));" in index
    assert "await redberry.provider.forceFlush();" in index
    assert "export const handler = redberryFlushAfter(async (event, context) => {" in index
    assert index.rstrip().endswith("});") and "withRedberry" not in index and "registerRedberry" not in index
    assert "temperature: 1," in index and "0.3" not in index
    deps = json.loads((node_fn / "package.json").read_text())["dependencies"]
    assert deps["@redberrylabs/otel"] == "^0.5.0" and "@ai-sdk/otel" in deps and deps["ai"] == "^7.0.148"
    assert deps["@opentelemetry/sdk-trace-base"] == "^2.0.0"
    # Nothing is set on AWS and nothing is registered: the code is the whole of it.
    assert not run.envs and "update-function-configuration" not in " ".join(run.kinds())
    assert "connect never deploys" in out and "needs no policy" in out
    assert "aws lambda update-function-code --function-name refund-agent" in out and "npm install --omit=dev" in out
    record = json.loads((node_fn / ".redberry" / "aws-lambda.json").read_text())
    assert record["agent_name"] == "refund-agent" and record["handler_file"] == "index.mjs"
    # Again: nothing more to change.
    before = {p: p.read_text() for p in node_fn.rglob("*.mjs")}
    rc, out = _lambda_connect(api, node_fn, "--yes", run=_Aws(function=_function()))
    assert rc == 0 and {p: p.read_text() for p in before} == before and "already wired" in out


def test_connect_moves_an_earlier_hasher_wiring_to_init(api, node_fn):
    (node_fn / "package.json").write_text(json.dumps({"name": "refund", "type": "module", "dependencies": {
        "ai": "^7.0.148", "@redberrylabs/otel": "^0.3.0"}}, indent=2) + "\n")
    (node_fn / "index.mjs").write_text(
        "// Redberry (redberry connect --platform aws-lambda): hashes, signs and stores each run of the handler\n"
        "// wrapped in withRedberry. Its settings are the function's REDBERRY_* environment variables.\n"
        "import { registerTelemetry } from 'ai';\nimport { OpenTelemetry } from '@ai-sdk/otel';\n"
        "import { registerRedberry, withRedberry } from '@redberrylabs/otel/node';\n\n"
        "registerRedberry({ serviceName: \"refund-agent\" });\n"
        "// AI SDK 7 emits no spans until an integration is registered.\n"
        "registerTelemetry(new OpenTelemetry({ runtimeContext: true }));\n\n"
        + INDEX_MJS.replace("export const handler = async (event, context) => {",
                            "export const handler = withRedberry(async (event, context) => {")
                   .rstrip().removesuffix(";") + ", { agentId: 'AGT-00000000000000000000000001' });\n")
    run = _Aws(function=_function(env={**SECRET_ENV, "REDBERRY_SIGNING_KEY": "k", "REDBERRY_STORE": "redberry"}))
    rc, out = _lambda_connect(api, node_fn, "--yes", run=run)
    index = (node_fn / "index.mjs").read_text()
    assert rc == 0 and "withRedberry" not in index and "registerRedberry" not in index and "hashes, signs" not in index
    assert "export const handler = redberryFlushAfter(async (event, context) => {" in index
    assert index.count("registerTelemetry(new OpenTelemetry") == 1 and index.count("from 'ai';") == 2   # kept once
    assert json.loads((node_fn / "package.json").read_text())["dependencies"]["@redberrylabs/otel"] == "^0.5.0"
    assert "REDBERRY_SIGNING_KEY, REDBERRY_STORE" in out and "remove them" in out


def test_connect_lambda_asks_first(api, node_fn):
    run = _Aws(function=_function())
    asked = []
    with pytest.raises(cli.CliError, match="nothing changed"):
        _lambda_connect(api, node_fn, run=run, ask=lambda q: asked.append(q) or False)
    assert asked == ["Apply these changes?"]
    assert "redberryFlushAfter" not in (node_fn / "index.mjs").read_text()
    assert not (node_fn / ".redberry").exists()


def test_iac_managed_functions_get_their_deploy_command(api, node_fn):
    (node_fn / "template.yaml").write_text("Transform: AWS::Serverless-2016-10-31\n")
    rc, out = _lambda_connect(api, node_fn, "--yes", run=_Aws(function=_function()))
    assert rc == 0 and "Next: sam build then sam deploy" in out


def test_a_container_image_gets_instructions(api, node_fn):
    fn = _function(PackageType="Image")
    del fn["Runtime"], fn["Handler"]
    rc, out = _lambda_connect(api, node_fn, "--yes", run=_Aws(function=fn))
    assert rc == 0 and "is a container image" in out and "init({ agent: \"refund-agent\" })" in out
    assert "@redberry_sdk.flush_after" in out and "redberry-sdk[providers]" in out
    assert "redberryFlushAfter" not in (node_fn / "index.mjs").read_text() and "--image-uri" in out


def test_node_handler_forms():
    text, how = awl.wrap_node_handler("exports.handler = async function (event) {\n  return 1;\n};\n", "handler")
    assert how == "exports" and text == "exports.handler = redberryFlushAfter(async function (event) {\n  return 1;\n});\n"
    text, how = awl.wrap_node_handler("export async function main(event, context) {\n  return 1;\n}\n", "main")
    assert how == "export function" and text.startswith("async function main(event, context) {")
    assert "const redberryMain = redberryFlushAfter(main);\nexport { redberryMain as main };" in text
    assert awl.wrap_node_handler(text, "main")[1] == "already"
    text, how = awl.wrap_node_handler("export const handler: Handler = middy(base)\n  .use(cors())\n\nconst x = 1;\n",
                                      "handler")
    assert text.startswith("export const handler: Handler = redberryFlushAfter(middy(base)\n  .use(cors()))\n")
    assert awl.wrap_node_handler("module.exports = { handler };\n", "handler")[1] is None
    assert awl.wrap_node_handler("export const handler = redberryFlushAfter(h);\n", "handler")[1] == "already"
    # The earlier connect's wrapping becomes the new one.
    text, _ = awl.wrap_node_handler("export const handler = withRedberry(h, { agentId: 'AGT-1' });\n", "handler")
    assert text == "export const handler = redberryFlushAfter(h);\n"
    old = ("async function main(e) {\n  return 1;\n}\n\n// Redberry: the handler Lambda calls is the wrapped one.\n"
           "const redberryMain = withRedberry(main, { agentId: 'AGT-1' });\nexport { redberryMain as main };\n")
    assert "const redberryMain = redberryFlushAfter(main);" in awl.wrap_node_handler(
        old.replace("async function main", "export async function main"), "main")[0]


def test_a_typescript_handler_gets_a_typed_wrapper(api, tmp_path):
    repo = tmp_path / "ts"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({"name": "ts", "type": "module", "dependencies": {"ai": "^7.0.0"}}))
    (repo / "src" / "index.ts").write_text("export const handler = async (event: unknown) => ({ statusCode: 200 });\n")
    rc, _ = _lambda_connect(api, repo, "--yes", run=_Aws(function=_function(handler="dist/index.handler")))
    text = (repo / "src" / "index.ts").read_text()
    assert rc == 0 and "<A extends unknown[], R>(handler: (...args: A) => R)" in text
    assert "export const handler = redberryFlushAfter(async (event: unknown) => ({ statusCode: 200 }));" in text


def test_handlers_are_found_compiled_and_nested(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export const handler = async () => 1;\n")
    assert awl.find_handler(tmp_path, "node", "dist/index.handler").file == (tmp_path / "src" / "index.ts").resolve()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text("def main(event, context):\n    return 1\n")
    h = awl.find_handler(tmp_path, "python", "pkg.api.main")
    assert h.file == (tmp_path / "pkg" / "api.py").resolve() and h.export == "main"
    assert awl.find_handler(tmp_path, "python", "missing.main").file is None


def test_lambda_flags(api, node_fn):
    with pytest.raises(cli.CliError, match="--function is for --platform aws-lambda"):
        cli.cmd_connect(cli.parser().parse_args(["--api", api, "connect", "--function", "f", "--name", "x"]))
    with pytest.raises(SystemExit):                                     # the hasher's stores are gone
        _lambda_connect(api, node_fn, "--yes", "--store", "s3", run=_Aws(function=_function()))
    with pytest.raises(cli.CliError, match="runs python3.12, but"):
        _lambda_connect(api, node_fn, "--yes", run=_Aws(function=_function(runtime="python3.12", handler="app.main")))
    with pytest.raises(cli.CliError, match="cannot find the handler 'main.handler'"):
        _lambda_connect(api, node_fn, "--yes", run=_Aws(function=_function(handler="main.handler")))
    with pytest.raises(cli.CliError, match="cannot read the Lambda function"):
        _lambda_connect(api, node_fn, "--yes", run=_Aws(fail="get-function-configuration"))
    with pytest.raises(cli.CliError, match="1 to 64 printable"):
        _lambda_connect(api, node_fn, "--yes", "--name", "x" * 65, run=_Aws(function=_function()))

# ── connect --platform aws-lambda: Python ───────────────────────────────────────────────────

APP_PY = '''\
"""Refunds."""
import json

from openai import OpenAI

client = OpenAI()


def lambda_handler(event, context):
    r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": event["body"]}])
    return {"statusCode": 200, "body": json.dumps({"text": r.choices[0].message.content})}
'''


@pytest.fixture
def py_fn(tmp_path):
    repo = tmp_path / "pyrefund"
    repo.mkdir()
    (repo / "app.py").write_text(APP_PY)
    (repo / "requirements.txt").write_text("openai")
    return repo


def test_connect_python_lambda(api, py_fn):
    run = _Aws(function=_function(handler="app.lambda_handler", runtime="python3.12"))
    rc, out = _lambda_connect(api, py_fn, "--yes", "--name", "refunds", run=run)
    assert rc == 0 and not run.envs
    app = (py_fn / "app.py").read_text()
    assert ("from openai import OpenAI\nimport redberry_sdk\n\n# Redberry: records the agent's model and tool calls"
            in app) and '\nredberry_sdk.init(agent="refunds")\n\nclient = OpenAI()' in app
    assert "@redberry_sdk.flush_after\ndef lambda_handler(event, context):" in app
    assert "create(temperature=1.0, model=" in app and "hasher" not in app
    assert (py_fn / "requirements.txt").read_text() == "openai\nredberry-sdk[providers]\n"
    assert "manylinux2014_aarch64" in out and "--python-version 3.12" in out
    compile(app, "app.py", "exec")
    rec = json.loads((py_fn / ".redberry" / "aws-lambda.json").read_text())
    assert rec["agent_name"] == "refunds"
    # A reconnect keeps the agent's name without --name, and changes nothing.
    rc, out = _lambda_connect(api, py_fn, "--yes", run=_Aws(function=_function(handler="app.lambda_handler",
                                                                                runtime="python3.12")))
    assert rc == 0 and (py_fn / "app.py").read_text() == app and "already wired" in out


def test_connect_moves_an_earlier_python_hasher_wiring_to_init(api, py_fn):
    (py_fn / "requirements.txt").write_text(
        "openai\nredberry-agent-sdk[hasher,providers] @ git+https://github.com/RedberryLabs/example.git@main\n")
    (py_fn / "app.py").write_text(APP_PY.replace(
        "from openai import OpenAI\n",
        "from openai import OpenAI\nfrom redberry_sdk.hasher import register, with_redberry\n\n"
        "# Redberry: hashes, signs and stores each run of the handler decorated with with_redberry.\n"
        "# Its settings are the function's REDBERRY_* environment variables.\nregister()\n").replace(
        "def lambda_handler", '@with_redberry(agent_id="AGT-1")\ndef lambda_handler'))
    rc, _ = _lambda_connect(api, py_fn, "--yes", run=_Aws(function=_function(handler="app.lambda_handler",
                                                                             runtime="python3.12")))
    app = (py_fn / "app.py").read_text()
    assert rc == 0 and "hasher" not in app and "register()" not in app and "with_redberry" not in app
    assert app.count("@redberry_sdk.flush_after\ndef lambda_handler") == 1
    assert 'redberry_sdk.init(agent="refund-agent")' in app and app.count("import redberry_sdk") == 1
    assert (py_fn / "requirements.txt").read_text() == "openai\nredberry-sdk[providers]\n"
    compile(app, "app.py", "exec")


def test_python_handler_assigned_from_an_adapter(api, tmp_path):
    repo = tmp_path / "mangum"
    repo.mkdir()
    (repo / "main.py").write_text("from mangum import Mangum\nfrom api import app\n\nhandler = Mangum(app)\n")
    (repo / "requirements.txt").write_text("mangum\n")
    rc, _ = _lambda_connect(api, repo, "--yes", run=_Aws(function=_function(handler="main.handler",
                                                                             runtime="python3.11")))
    text = (repo / "main.py").read_text()
    assert rc == 0 and "handler = redberry_sdk.flush_after(Mangum(app))\n" in text
    assert 'redberry_sdk.init(agent="refund-agent")' in text

def test_connect_leaves_vendored_packages_alone(tmp_path):
    # A Lambda zip folder holds the app's code next to `pip install --target package` output; connect must only
    # read and edit the app's own files (the live test found it editing package/openai/lib/*.py).
    from redberry_cli import azure_functions as azf
    (tmp_path / "lambda_function.py").write_text("import openai\n")
    (tmp_path / "package" / "openai" / "lib").mkdir(parents=True)
    (tmp_path / "package" / "openai" / "lib" / "computer.py").write_text("client.chat.completions.create(model='x')\n")
    (tmp_path / "package" / "openai-1.99.0.dist-info").mkdir()
    (tmp_path / "python" / "requests").mkdir(parents=True)        # a layer-style folder
    (tmp_path / "python" / "requests" / "api.py").write_text("x = 1\n")
    (tmp_path / "python" / "requests-2.0.dist-info").mkdir()
    files = [p.relative_to(tmp_path).as_posix() for p in azf._py_files(tmp_path)]
    assert files == ["lambda_function.py"]
