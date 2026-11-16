"""`redberry setup-azure` and `redberry connect --platform azure-functions`, against a faked `az`. No Azure
resource is created or changed, and no Redberry API is called: connect only wires the app's code (``init()``
finds the installation with the app's managed identity).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from redberry_cli import azure_functions as azf
from redberry_cli import azure_setup
from redberry_cli import main as cli

SUB, TENANT = "00000000-0000-0000-0000-0000000000aa", "11111111-1111-1111-1111-1111111111bb"
CLIENT, PRINCIPAL = "22222222-2222-2222-2222-2222222222cc", "33333333-3333-3333-3333-3333333333dd"
IDENTITY_ID = (f"/subscriptions/{SUB}/resourceGroups/rg-redberry/providers/Microsoft.ManagedIdentity/"
               "userAssignedIdentities/redberry-trace-writer")
CONTAINER = "https://acmeruns.blob.core.windows.net/redberry-runs"


@pytest.fixture(autouse=True)
def _az_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AZ", "az")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


class _Az:
    """The faked ``az``. ``fail``: a substring of a command that fails; ``exists``: commands that answer "already
    exists"; ``unseen``: how many times a role assignment fails first because the new identity is not visible."""

    def __init__(self, fail=None, exists=(), unseen=0, group=False, runtime="Node|22"):
        self.calls: list[list[str]] = []
        self.settings: list[dict] = []
        self.fail, self.exists, self.unseen, self.group, self.runtime = fail, set(exists), unseen, group, runtime

    def __call__(self, cmd, cwd=None, input=None, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        r = type("R", (), {})()
        r.returncode, r.stdout, r.stderr = 0, "", ""
        line = " ".join(cmd)
        if cmd[1:3] == ["account", "show"]:
            r.stdout = TENANT if "tenantId" in cmd else json.dumps({"id": SUB, "tenantId": TENANT, "name": "Acme"})
        elif cmd[1:3] == ["group", "exists"]:
            r.stdout = "true\n" if self.group else "false\n"
        elif cmd[1:3] == ["identity", "create"] or cmd[1:3] == ["identity", "show"]:
            r.stdout = json.dumps({"id": IDENTITY_ID, "clientId": CLIENT, "principalId": PRINCIPAL})
        elif "immutability-policy" in cmd and "show" in cmd:
            r.stdout = json.dumps({"etag": '"8d9f"', "state": "Unlocked"})
        elif cmd[1:3] == ["ad", "signed-in-user"]:
            r.stdout = "44444444-4444-4444-4444-4444444444ee\n"
        elif cmd[1:3] == ["functionapp", "show"]:
            r.stdout = json.dumps({"id": f"/subscriptions/{SUB}/resourceGroups/rg-apps/providers/Microsoft.Web/sites/"
                                         "acme-agents", "siteConfig": {"linuxFxVersion": self.runtime}})
        elif cmd[1:4] == ["functionapp", "config", "appsettings"]:
            path = cmd[cmd.index("--settings") + 1]
            assert path.startswith("@")
            self.settings.append({"mode": stat.S_IMODE(Path(path[1:]).stat().st_mode),
                                  "rows": json.loads(Path(path[1:]).read_text())})
        if cmd[1:3] == ["role", "assignment"] and self.unseen > 0:
            self.unseen -= 1
            r.returncode, r.stderr = 1, f"(PrincipalNotFound) Principal {PRINCIPAL} does not exist in the directory."
        if self.fail and self.fail in line:
            r.returncode, r.stderr = 1, "ERROR: boom"
        if any(e in line for e in self.exists):
            r.returncode, r.stderr = 1, "ERROR: (Conflict) The resource already exists."
        return r

    def kinds(self) -> list[str]:
        return [" ".join(c[1:4]) for c in self.calls]


# ── setup-azure ─────────────────────────────────────────────────────────────────────────────


def _setup(*argv, run=None, slept=None):
    args = cli.parser().parse_args(["setup-azure", "--resource-group", "rg-redberry", "--location", "westeurope",
                                    "--account", "acmeruns", *argv])
    lines: list[str] = []
    rc = azure_setup.cmd_setup_azure(args, out=lines.append, run=run,
                                     sleep=(slept.append if slept is not None else lambda s: None))
    return rc, "\n".join(lines)


def test_setup_azure_prints_the_commands_for_a_vercel_project(tmp_path):
    rc, out = _setup("--vercel-team", "acme", "--vercel-project", "acme-support", "--repo", str(tmp_path), "--print")
    assert rc == 0
    assert ("az storage account create --name acmeruns --resource-group rg-redberry --location westeurope --kind "
            "StorageV2 --min-tls-version TLS1_2 --https-only true --allow-blob-public-access false "
            "--allow-shared-key-access false") in out
    assert "az storage container-rm create --storage-account acmeruns --resource-group rg-redberry --name redberry-runs" in out
    assert "immutability-policy create --account-name acmeruns --resource-group rg-redberry " \
           "--container-name redberry-runs --period 2557 --allow-protected-append-writes false" in out
    assert "immutability-policy lock" not in out                                    # unlocked by default
    assert ('az role assignment create --assignee-object-id "${PRINCIPAL_ID}" --assignee-principal-type '
            'ServicePrincipal --role \'Storage Blob Data Contributor\' --scope /subscriptions/"${SUBSCRIPTION_ID}"'
            '/resourceGroups/rg-redberry/providers/Microsoft.Storage/storageAccounts/acmeruns/blobServices/default/'
            'containers/redberry-runs') in out
    assert ("az identity federated-credential create --name vercel-acme-acme-support --identity-name "
            "redberry-trace-writer --resource-group rg-redberry --issuer https://oidc.vercel.com/acme --subject "
            "owner:acme:project:acme-support:environment:production --audiences https://vercel.com/acme") in out
    assert f"# then: the container {CONTAINER} is ready for its writer" in out
    assert not (tmp_path / ".redberry").exists()


def test_setup_azure_retention_is_whole_days(tmp_path):
    with pytest.raises(azure_setup.SetupError, match="shortest immutability period is one day"):
        _setup("--vercel-team", "acme", "--vercel-project", "p", "--retention", "30m", "--print")
    with pytest.raises(azure_setup.SetupError, match="--retention"):
        _setup("--vercel-team", "acme", "--vercel-project", "p", "--retention", "forever", "--print")
    _rc, out = _setup("--vercel-team", "acme", "--vercel-project", "p", "--retention", "1d", "--print")
    assert "--period 1 " in out
    assert azure_setup.retention_days("7y") == 2557 and azure_setup.retention_days("90d") == 90


def test_setup_azure_runs_them_and_records_what_it_made(tmp_path):
    run, slept = _Az(unseen=2, exists=("container-rm create",)), []
    rc, out = _setup("--vercel-team", "acme", "--vercel-project", "acme-support", "--repo", str(tmp_path), run=run,
                     slept=slept)
    assert rc == 0 and "already there" in out
    assert slept == [5, 5] and out.count("not visible yet") == 2                # the new identity, twice
    assert [k for k in run.kinds() if not k.startswith("provider ")] == ["account show -o", "group exists --name",
                           "group create --name", "storage account create", "storage container-rm create",
                           "storage container immutability-policy", "identity create --name",
                           "role assignment create", "role assignment create", "role assignment create",
                           "identity federated-credential create"]
    assert all(c[0] == "az" for c in run.calls)
    record = json.loads((tmp_path / ".redberry" / "azure.json").read_text())
    assert record["container_url"] == CONTAINER and record["tenant_id"] == TENANT
    assert record["identity"] == {"name": "redberry-trace-writer", "id": IDENTITY_ID, "client_id": CLIENT,
                                  "principal_id": PRINCIPAL}
    assert record["oidc_subject"] == "owner:acme:project:acme-support:environment:production"
    assert record["retention_days"] == 2557 and record["locked"] is False
    assert f"Next: the container {CONTAINER} is ready for its writer" in out
    assert "lock it (irreversible)" in out


def test_setup_azure_locks_only_when_told(tmp_path):
    run = _Az(group=True)
    rc, out = _setup("--function-app", "acme-agents", "--app-resource-group", "rg-apps", "--lock",
                     "--repo", str(tmp_path), run=run)
    assert rc == 0 and "group create --name" not in run.kinds()                 # the group was there
    lock = next(c for c in run.calls if "lock" in c)
    assert lock[lock.index("--if-match") + 1] == '"8d9f"'
    assign = next(c for c in run.calls if c[1:3] == ["functionapp", "identity"])
    assert assign[assign.index("--identities") + 1] == IDENTITY_ID and assign[assign.index("--name") + 1] == "acme-agents"
    assert assign[assign.index("--resource-group") + 1] == "rg-apps"
    assert not any("federated-credential" in c for c in run.calls)
    assert f"Next: the container {CONTAINER} is ready for its writer" in out
    assert json.loads((tmp_path / ".redberry" / "azure.json").read_text())["locked"] is True


def test_setup_azure_evidence_makes_three_containers_one_immutable(tmp_path):
    run = _Az()
    rc, out = _setup("--evidence", "--repo", str(tmp_path), "--reader-for-me", run=run)
    assert rc == 0
    made = [c[c.index("--name") + 1] for c in run.calls if "container-rm" in c]
    assert made == ["valp-evidence", "valp-payloads", "valp-index"]
    immutable = [c[c.index("--container-name") + 1] for c in run.calls if "immutability-policy" in c]
    assert immutable == ["valp-evidence"]                          # the sink rewrites the index, erases payloads
    (props,) = [c for c in run.calls if "blob-service-properties" in c]
    assert props[props.index("--enable-delete-retention") + 1] == "false"
    assert props[props.index("--enable-versioning") + 1] == "false"
    writers = [c for c in run.calls if "Storage Blob Data Contributor" in c]
    readers = [c for c in run.calls if "Storage Blob Data Reader" in c]
    assert len(writers) == 3 and len(readers) == 3
    assert all(c[c.index("--assignee-principal-type") + 1] == "User" for c in readers)
    url = "https://acmeruns.blob.core.windows.net/valp-"
    assert (f"the containers {url}evidence, {url}payloads and {url}index are ready (identity {CLIENT}, "
            f"tenant {TENANT})") in out
    assert "az vm identity assign" in out and "redberry-valp-sink" in json.dumps(
        json.loads((tmp_path / ".redberry" / "azure.json").read_text()))
    with pytest.raises(azure_setup.SetupError, match="drop --container"):
        _setup("--evidence", "--container", "mine", "--print")


def test_setup_azure_needs_exactly_one_target_and_valid_names(tmp_path):
    with pytest.raises(azure_setup.SetupError, match="one of: --vercel-team"):
        _setup("--print")
    with pytest.raises(azure_setup.SetupError, match="one of"):
        _setup("--evidence", "--function-app", "x", "--print")
    with pytest.raises(azure_setup.SetupError, match="--vercel-project"):
        _setup("--vercel-team", "acme", "--repo", str(tmp_path), "--print")
    args = cli.parser().parse_args(["setup-azure", "--resource-group", "g", "--location", "l", "--account",
                                    "Bad_Name", "--evidence", "--print"])
    with pytest.raises(azure_setup.SetupError, match="--account"):
        azure_setup.cmd_setup_azure(args, out=lambda _m: None)


def test_setup_azure_stops_on_a_real_failure(tmp_path):
    with pytest.raises(azure_setup.SetupError, match="storage account create failed: ERROR: boom"):
        _setup("--evidence", "--repo", str(tmp_path), run=_Az(fail="storage account create"))
    assert not (tmp_path / ".redberry" / "azure.json").exists()


def test_az_binary_falls_back_to_the_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("AZ")
    monkeypatch.setattr(azure_setup.shutil, "which", lambda _n: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert azure_setup.az_binary() == "az"
    venv = tmp_path / ".azure-cli-venv" / "bin" / "az"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    assert azure_setup.az_binary() == str(venv)
    monkeypatch.setenv("AZ", "/opt/az")
    assert azure_setup.az_binary() == "/opt/az"


def test_container_urls():
    assert azure_setup.container_url(CONTAINER + "/") == CONTAINER
    for bad in (CONTAINER + "?sv=2024&sig=x", "https://acme.blob.core.windows.net", "gs://bucket",
                "https://Acme.blob.core.windows.net/runs"):
        with pytest.raises(azure_setup.SetupError):
            azure_setup.container_url(bad)


# ── connect --platform azure-functions: Node.js ─────────────────────────────────────────────

REFUND_TS = """\
import { app, type HttpRequest, type InvocationContext } from '@azure/functions';
import { generateText } from 'ai';

async function refund(request: HttpRequest, context: InvocationContext) {
  const { text } = await generateText({
    model: 'openai/gpt-4o-mini',
    prompt: await request.text(),
    temperature: 0.3,
  });
  return { jsonBody: { text } };
}

app.http('refund', { methods: ['POST'], authLevel: 'function', handler: refund });
"""

TRIAGE_TS = """\
import { app } from '@azure/functions';
import { classify } from '../lib/triage';

app.http('triage', {
  methods: ['POST'],
  handler: async (request, context) => {
    return { jsonBody: await classify(await request.text()) };
  },
});

app.get('health', async () => ({ body: 'ok' }));
"""

LIB_TS = """\
import { generateText } from 'ai';

export async function classify(text: string) {
  const r = await generateText({ model: 'openai/gpt-4o-mini', prompt: text });
  return { label: r.text };
}
"""


@pytest.fixture
def node_app(tmp_path):
    repo = tmp_path / "functions"
    (repo / "src" / "functions").mkdir(parents=True)
    (repo / "src" / "lib").mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "acme-agents", "type": "module", "main": "dist/src/functions/*.js",
        "dependencies": {"@azure/functions": "^4.7.0", "ai": "^7.0.148"}}, indent=2) + "\n")
    (repo / "host.json").write_text(json.dumps({"version": "2.0", "functionTimeout": "00:05:00"}))
    (repo / "src" / "functions" / "refund.ts").write_text(REFUND_TS)
    (repo / "src" / "functions" / "triage.ts").write_text(TRIAGE_TS)
    (repo / "src" / "lib" / "triage.ts").write_text(LIB_TS)
    return repo


def _fn_connect(api, repo, *argv, run=None, ask=None):
    args = cli.parser().parse_args(["--api", api, "connect", "--platform", "azure-functions", "--repo", str(repo),
                                    "--app", "acme-agents", "--resource-group", "rg-apps", *argv])
    lines: list[str] = []
    if ask is None:
        rc = cli.cmd_connect(args, out=lines.append, run=run)
    else:
        rc = azf.cmd_connect(args, out=lines.append, run=run, ask=ask)
    return rc, "\n".join(lines)


def test_node_functions_and_the_calls_they_reach(node_app):
    fns = {fn.name: fn for fn in azf.node_functions(node_app)}
    assert set(fns) == {"refund", "triage", "health"}
    assert [c.file.name for c in fns["refund"].calls] == ["refund.ts"]
    assert [c.file.name for c in fns["triage"].calls] == ["triage.ts"] and fns["triage"].calls[0].file.parent.name == "lib"
    assert fns["health"].calls == []                       # same file as triage, but it calls no model




@pytest.fixture
def api():
    """No Redberry API is called on this path: an address nothing listens on."""
    return "http://127.0.0.1:9"


def test_connect_node_app(api, node_app):
    run = _Az()
    rc, out = _fn_connect(api, node_app, "--yes", run=run)
    assert rc == 0
    deps = json.loads((node_app / "package.json").read_text())["dependencies"]
    assert deps["@redberrylabs/otel"] == "^0.5.0" and deps["ai"] == "^7.0.148"
    assert {"@ai-sdk/otel", "@opentelemetry/api", "@opentelemetry/sdk-trace-base",
            "@opentelemetry/context-async-hooks", "@opentelemetry/resources"} <= set(deps)
    setup = (node_app / "src" / "redberry.ts").read_text()
    assert "import { init } from '@redberrylabs/otel/init';" in setup
    assert 'init({ agent: "acme-agents" });' in setup                        # the app's name by default
    assert "registerTelemetry(new OpenTelemetry({ runtimeContext: true }));" in setup
    refund = (node_app / "src" / "functions" / "refund.ts").read_text()
    assert refund.startswith("import '../redberry.js';\nimport { app,")
    assert "handler: refund });" in refund and "withRedberry" not in refund
    assert "temperature: 1," in refund and "0.3" not in refund
    triage = (node_app / "src" / "functions" / "triage.ts").read_text()
    assert triage.startswith("import '../redberry.js';\n") and "withRedberry" not in triage
    assert "temperature: 1," in (node_app / "src" / "lib" / "triage.ts").read_text()
    # Nothing is set on Azure: no app settings, no deployment.
    assert not run.settings and [c[1:3] for c in run.calls] == [["functionapp", "show"]]
    record = json.loads((node_app / ".redberry" / "azure-functions.json").read_text())
    assert record["agent_name"] == "acme-agents" and [f["function"] for f in record["functions"]] == ["refund", "triage"]
    assert "managed identity" in out
    assert out.rstrip().endswith("Next: npm install then func azure functionapp publish acme-agents. "
                                 "connect never deploys.")
    # again: nothing more to change
    before = {p: p.read_text() for p in node_app.rglob("*") if p.is_file() and ".redberry" not in p.parts}
    rc, out = _fn_connect(api, node_app, "--yes", run=_Az())
    assert rc == 0 and "already wired" in out
    assert {p: p.read_text() for p in before} == before


def test_connect_node_asks_first(api, node_app):
    before = {p: p.read_text() for p in node_app.rglob("*") if p.is_file()}
    with pytest.raises(cli.CliError, match="nothing changed"):
        _fn_connect(api, node_app, run=_Az())
    assert {p: p.read_text() for p in node_app.rglob("*") if p.is_file()} == before
    rc, out = _fn_connect(api, node_app, run=_Az(), ask=lambda q: True)
    assert rc == 0 and "+++ b/src/redberry.ts" in out


def test_connect_moves_an_earlier_node_wiring_to_init(api, node_app):
    (node_app / "src" / "redberry.ts").write_text(
        "import { registerTelemetry } from 'ai';\nimport { OpenTelemetry } from '@ai-sdk/otel';\n"
        "import { registerRedberry } from '@redberrylabs/otel/node';\n\n"
        "registerRedberry({ serviceName: \"acme-agents\" });\n"
        "registerTelemetry(new OpenTelemetry({ runtimeContext: true }));\n")
    refund = (node_app / "src" / "functions" / "refund.ts")
    refund.write_text("import '../redberry.js';\nimport { withRedberry } from '@redberrylabs/otel/node';\n"
                      + REFUND_TS.replace("handler: refund", "handler: withRedberry(refund, { agentId: 'AGT-1' })"))
    rc, _ = _fn_connect(api, node_app, "--yes", "--name", "support", run=_Az())
    text = refund.read_text()
    assert rc == 0 and "withRedberry" not in text and "handler: refund });" in text
    assert text.count("import '../redberry.js';") == 1
    setup = (node_app / "src" / "redberry.ts").read_text()
    assert "registerRedberry" not in setup and 'init({ agent: "support" });' in setup


def test_names_and_flags(api, node_app):
    rc, out = _fn_connect(api, node_app, "--yes", "--name", "status=health,status=triage", run=_Az())
    assert rc == 0 and 'init({ agent: "status" });' in (node_app / "src" / "redberry.ts").read_text()
    assert "import '../redberry.js';" not in (node_app / "src" / "functions" / "refund.ts").read_text()
    with pytest.raises(cli.CliError, match="no HTTP function nope"):
        _fn_connect(api, node_app, "--yes", "--name", "a=nope", run=_Az())
    with pytest.raises(cli.CliError, match="give the app one name"):
        _fn_connect(api, node_app, "--yes", "--name", "a=refund,b=triage", run=_Az())
    args = cli.parser().parse_args(["connect", "--platform", "azure-functions", "--repo", str(node_app)])
    with pytest.raises(cli.CliError, match="--app"):
        cli.cmd_connect(args, out=lambda _m: None)
    args = cli.parser().parse_args(["connect", "--app", "x", "--name", "x"])
    with pytest.raises(cli.CliError, match="--app is for --platform azure-functions"):
        cli.cmd_connect(args, out=lambda _m: None)
    with pytest.raises(SystemExit):                                     # the hasher's stores are gone
        _fn_connect(api, node_app, "--yes", "--store", "azure-blob", run=_Az())


def test_app_checks(api, tmp_path, node_app):
    pkg = json.loads((node_app / "package.json").read_text())
    pkg["dependencies"]["@azure/functions"] = "^3.5.0"
    (node_app / "package.json").write_text(json.dumps(pkg))
    with pytest.raises(cli.CliError, match="v4 programming model"):
        _fn_connect(api, node_app, "--yes", run=_Az())
    with pytest.raises(cli.CliError, match="not an Azure Functions app"):
        _fn_connect(api, tmp_path, "--yes", run=_Az())
    pkg["dependencies"]["@azure/functions"] = "^4.0.0"
    (node_app / "package.json").write_text(json.dumps(pkg))
    with pytest.raises(cli.CliError, match="cannot find the function app.*boom"):
        _fn_connect(api, node_app, "--yes", run=_Az(fail="functionapp show"))


def test_a_commonjs_app_without_ai_sdk_7_is_warned(api, tmp_path):
    repo = tmp_path / "cjs"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({"dependencies": {"@azure/functions": "^4.0.0", "ai": "^6.0.0"}}))
    (repo / "src" / "chat.js").write_text(
        "const { app } = require('@azure/functions');\nconst { generateText } = require('ai');\n\n"
        "app.http('chat', { handler: async (req) => {\n  const r = await generateText({ model: 'm', prompt: 'p' });\n"
        "  return { body: r.text };\n} });\n")
    rc, out = _fn_connect(api, repo, "--yes", run=_Az())
    assert rc == 0 and "AI SDK 6" in out
    assert (repo / "src" / "chat.js").read_text().startswith("require('./redberry.js');\nconst { app }")
    assert "require('@redberrylabs/otel/init')" in (repo / "src" / "redberry.js").read_text()


def test_the_azure_monitor_distro_keeps_its_provider(api, node_app):
    pkg = json.loads((node_app / "package.json").read_text())
    pkg["dependencies"]["@azure/monitor-opentelemetry"] = "^1.8.0"
    (node_app / "package.json").write_text(json.dumps(pkg))
    rc, out = _fn_connect(api, node_app, "--yes", run=_Az())
    assert rc == 0 and "redberryExporter()" in out
    assert "init({" not in (node_app / "src" / "redberry.ts").read_text()
# ── connect --platform azure-functions: Python ──────────────────────────────────────────────

FUNCTION_APP_PY = '''\
"""The agents."""
import azure.functions as func
from openai import OpenAI

from agent import answer

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
client = OpenAI()


@app.function_name(name="refund")
@app.route(route="refund", methods=["POST"])
def refund_handler(req: func.HttpRequest) -> func.HttpResponse:
    r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": req.get_body().decode()}])
    return func.HttpResponse(r.choices[0].message.content)


@app.route(route="support")
def support(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(answer(req.get_body().decode()))


@app.route(route="health")
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("ok")
'''

AGENT_PY = '''\
import anthropic

client = anthropic.Anthropic()


def answer(text: str) -> str:
    r = client.messages.create(model="claude-x", max_tokens=200, temperature=0.2,
                               messages=[{"role": "user", "content": text}])
    return r.content[0].text
'''


@pytest.fixture
def py_app(tmp_path):
    repo = tmp_path / "pyfunctions"
    repo.mkdir()
    (repo / "function_app.py").write_text(FUNCTION_APP_PY)
    (repo / "agent.py").write_text(AGENT_PY)
    (repo / "requirements.txt").write_text("azure-functions\nopenai\nanthropic")
    (repo / "host.json").write_text(json.dumps({"version": "2.0"}))
    return repo


def test_python_functions_and_the_calls_they_reach(py_app):
    fns = {fn.name: fn for fn in azf.python_functions(py_app)}
    assert set(fns) == {"refund", "support", "health"}
    assert fns["refund"].calls and fns["support"].calls and fns["health"].calls == []




def test_connect_python_app(api, py_app, monkeypatch):
    from redberry_cli import instrument
    # anthropic 1.x: temperature is added via extra_body. On 2.x (API 2026-10-21) the call is left
    # alone, which test_cli.py covers; CI installs whichever is latest.
    monkeypatch.setattr(instrument, "_anthropic_major", lambda: 1)
    run = _Az(runtime="Python|3.11")
    rc, out = _fn_connect(api, py_app, "--yes", "--name", "support-desk", run=run)
    assert rc == 0 and not run.settings
    reqs = (py_app / "requirements.txt").read_text()
    assert reqs == "azure-functions\nopenai\nanthropic\nredberry-sdk[providers]\n"
    app = (py_app / "function_app.py").read_text()
    assert ("from agent import answer\nimport redberry_sdk\n\n# Redberry: records" in app
            and '\nredberry_sdk.init(agent="support-desk")\n\napp = func.FunctionApp(' in app)
    assert '@app.route(route="refund", methods=["POST"])\n@redberry_sdk.flush_after\ndef refund_handler(' in app
    assert '@app.route(route="support")\n@redberry_sdk.flush_after\ndef support(' in app
    assert '@app.route(route="health")\ndef health(' in app
    assert "create(temperature=1.0, model=" in app and "hasher" not in app
    assert "temperature=1.0" in (py_app / "agent.py").read_text() and "0.2" not in (py_app / "agent.py").read_text()
    assert "Next: func azure functionapp publish acme-agents" in out and "private repository" not in out
    compile(app, "function_app.py", "exec")
    # again: nothing more is added
    before = {p: p.read_text() for p in py_app.glob("*.py")}
    rc, out = _fn_connect(api, py_app, "--yes", run=_Az(runtime="Python|3.11"))
    assert {p: p.read_text() for p in before} == before and (py_app / "requirements.txt").read_text() == reqs
    assert 'init(agent="support-desk")' in (py_app / "function_app.py").read_text()      # the recorded name


def test_connect_moves_an_earlier_python_hasher_wiring_to_init(api, py_app):
    (py_app / "requirements.txt").write_text(
        "azure-functions\nredberry-agent-sdk[hasher,providers] @ git+https://github.com/RedberryLabs/example.git@main\n")
    (py_app / "function_app.py").write_text(FUNCTION_APP_PY.replace(
        "from agent import answer\n",
        "from agent import answer\nfrom redberry_sdk.hasher import register, with_redberry\n\n"
        "# Redberry: hashes, signs and stores each run of the functions decorated with with_redberry. Its\n"
        "# settings are the REDBERRY_* app settings (redberry connect --platform azure-functions).\nregister()\n")
        .replace("def refund_handler", '@with_redberry(agent_id="AGT-1")\ndef refund_handler'))
    rc, _ = _fn_connect(api, py_app, "--yes", run=_Az(runtime="Python|3.11"))
    app = (py_app / "function_app.py").read_text()
    assert rc == 0 and "hasher" not in app and "register()" not in app and "with_redberry" not in app
    assert app.count("@redberry_sdk.flush_after\ndef refund_handler") == 1
    assert app.count("import redberry_sdk") == 1 and 'redberry_sdk.init(agent="acme-agents")' in app
    assert (py_app / "requirements.txt").read_text() == "azure-functions\nredberry-sdk[providers]\n"
    compile(app, "function_app.py", "exec")


def test_python_names_a_function_the_reader_cannot_see(api, tmp_path):
    repo = tmp_path / "one"
    repo.mkdir()
    (repo / "function_app.py").write_text(
        "import azure.functions as func\nfrom my_framework import run\n\napp = func.FunctionApp()\n\n\n"
        "@app.route(route=\"ask\")\nasync def ask(req: func.HttpRequest) -> func.HttpResponse:\n"
        "    return func.HttpResponse(await run(req))\n")
    rc, out = _fn_connect(api, repo, "--yes", "--name", "concierge", run=_Az(runtime="Python|3.12"))
    text = (repo / "function_app.py").read_text()
    assert rc == 0 and 'redberry_sdk.init(agent="concierge")' in text
    assert "flush_after" not in text and "ask is async" in out              # flush_after wraps plain functions
    assert (repo / "requirements.txt").read_text() == azf.PY_REQUIREMENT + "\n"
def test_setup_registers_missing_resource_providers():
    # A new subscription has Microsoft.KeyVault and others unregistered; setup registers them first.
    calls = []

    class R:
        def __init__(self, stdout="", rc=0):
            self.stdout, self.returncode, self.stderr = stdout, rc, ""

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["provider", "show"]:
            return R("Registered" if cmd[4] == "Microsoft.Storage" else "NotRegistered")
        return R()

    out = []
    azure_setup.ensure_providers(run, azure_setup.PROVIDERS + ("Microsoft.KeyVault",), [], out.append)
    registered = [c[4] for c in calls if c[1:3] == ["provider", "register"]]
    assert "Microsoft.Storage" not in registered and "Microsoft.KeyVault" in registered
    assert all("--wait" in c for c in calls if c[1:3] == ["provider", "register"])
