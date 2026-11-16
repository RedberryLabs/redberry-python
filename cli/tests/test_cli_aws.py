"""`redberry connect --platform aws-lambda`, against a faked `aws` (as
test_cli_azure.py does for `az`). No AWS resource is created or changed, and no Redberry API is called:
connect only wires the function's code (``init()`` finds the installation from the function's own credentials).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from redberry_cli import aws_lambda as awl
from redberry_cli import _cloud
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




# ── the aws CLI ─────────────────────────────────────────────────────────────────────────────


def test_aws_binary_falls_back_to_the_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS")
    monkeypatch.setattr(_cloud.shutil, "which", lambda _n: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _cloud.aws_binary() == "aws"
    venv = tmp_path / ".aws-cli-venv" / "bin" / "aws"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    assert _cloud.aws_binary() == str(venv)
    monkeypatch.setattr(_cloud.shutil, "which", lambda _n: "/usr/local/bin/aws")
    assert _cloud.aws_binary() == "/usr/local/bin/aws"
    monkeypatch.setenv("AWS", "/opt/aws")
    assert _cloud.aws_binary() == "/opt/aws"




def test_regions_are_checked():
    assert _cloud.region("eu-west-1") == "eu-west-1" and _cloud.region("us-gov-west-1") == "us-gov-west-1"
    for bad in ("", "eu-west", "EU-WEST-1", "europe"):
        with pytest.raises(_cloud.SetupError, match="an AWS region"):
            _cloud.region(bad)


def test_a_missing_aws_cli_says_how_to_install_it():
    def run(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    with pytest.raises(_cloud.SetupError, match="AWS CLI \\(aws\\) is not installed"):
        _cloud.aws(run, ["aws", "sts", "get-caller-identity"])


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
