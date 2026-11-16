"""`redberry connect --platform vercel`: a Next.js app on the Vercel AI SDK, wired to the ingest exporter.

No Redberry API is called and nothing is deployed: connect writes ``instrumentation.ts`` (``registerOTel`` with
``redberryExporter(exporterOptionsFrom({}))``), the dependencies and ``temperature: 1``, and sets the REDBERRY_*
variables the install printed for ``--vercel-team`` on the project with a faked `vercel` CLI.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from redberry_cli import _edits as ed
from redberry_cli import _ts_tools as ts
from redberry_cli import main as cli
from redberry_cli import vercel

PROJECT, PROJECT_ID, TEAM_ID = "acme-support", "prj_123", "team_abc"

#: What the GCP install prints for --vercel-team, as a person would paste it.
GCP_OUTPUT = """\
Vercel environment variables for team acme (not secret):
  REDBERRY_CLOUD=gcp
  REDBERRY_INGEST_URL=https://redberry-ingest-123456789012.europe-west2.run.app
  REDBERRY_GCP_PROJECT_NUMBER=123456789012
  REDBERRY_GCP_POOL_ID=redberry-vercel
  REDBERRY_GCP_PROVIDER_ID=vercel-acme
  REDBERRY_GCP_SERVICE_ACCOUNT=redberry-vercel-sender@acme-redberry.iam.gserviceaccount.com
  Set each in the Vercel project, e.g.: printf %s https://x | vercel env add REDBERRY_INGEST_URL production
"""
AWS_OUTPUT = """\
==> Set these environment variables in your Vercel project(s) (Settings > Environment Variables), with OIDC
==>   REDBERRY_CLOUD=aws
==>   REDBERRY_INGEST_URL=https://abc.lambda-url.eu-west-1.on.aws
==>   REDBERRY_AWS_REGION=eu-west-1
==>   REDBERRY_AWS_ROLE_ARN=arn:aws:iam::123456789012:role/redberry-vercel-sender
"""


HANDLER = """\
import { streamText, tool } from 'ai';
import { z } from 'zod';
import { tools } from './tools';

export async function handle(req: Request) {
  const { prompt } = await req.json();
  const result = streamText({
    model: 'openai/gpt-4o-mini',
    system: 'You are the refunds agent.',
    prompt,
    tools,
    temperature: 0.2,
  });
  return result.toUIMessageStreamResponse();
}
"""

TOOLS = """\
import { tool } from 'ai';
import { z } from 'zod';

export const tools = {
  lookup_customer: tool({
    description: 'Find a customer by email or id.',
    inputSchema: z.object({ email: z.string().optional(), customer_id: z.string() }),
    execute: async ({ email }) => ({ email }),
  }),
  issue_refund: tool({
    description: "Issue a refund. " + "Irreversible.",
    inputSchema: z.object({
      amount: z.number().describe('Amount in GBP.'),
      reason: z.enum(['damaged', 'late']),
      tags: z.array(z.string()).nullable(),
    }),
    execute: async () => ({ ok: true }),
  }),
  weird: tool({
    description: `Uses a custom schema`,
    inputSchema: mySchema,
    execute: async () => null,
  }),
};
"""

CHAT = """\
import { generateText } from 'ai';

export async function POST(req: Request) {
  const { text } = await generateText({
    model: 'google/gemini-2.5-flash-lite',
    prompt: 'hi',
  });
  return Response.json({ text });
}
"""


@pytest.fixture
def app(tmp_path):
    repo = tmp_path / "app"
    (repo / "app" / "api" / "refund").mkdir(parents=True)
    (repo / "app" / "api" / "(public)" / "chat").mkdir(parents=True)
    (repo / "app" / "api" / "health").mkdir(parents=True)
    (repo / "lib").mkdir()
    (repo / ".vercel").mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "acme-support", "private": True,
        "dependencies": {"ai": "^7.0.148", "next": "^16.4.2", "zod": "^4.6.7"}}, indent=2) + "\n")
    (repo / "tsconfig.json").write_text('{\n  // comment\n  "compilerOptions": {"paths": {"@/*": ["./*"]},},\n}\n')
    (repo / ".vercel" / "project.json").write_text(json.dumps({"projectId": PROJECT_ID, "orgId": TEAM_ID,
                                                               "projectName": PROJECT}))
    (repo / "app" / "api" / "refund" / "route.ts").write_text(
        "import { handle } from '@/lib/handler';\nexport const POST = handle;\n")
    (repo / "app" / "api" / "(public)" / "chat" / "route.ts").write_text(CHAT)
    (repo / "app" / "api" / "health" / "route.ts").write_text("export const GET = () => Response.json({});\n")
    (repo / "lib" / "handler.ts").write_text(HANDLER)
    (repo / "lib" / "tools.ts").write_text(TOOLS)
    (repo / "node_modules" / "x").mkdir(parents=True)
    (repo / "node_modules" / "x" / "index.ts").write_text("import { streamText } from 'ai'; streamText({});\n")
    return repo




class _Run:
    """The faked `vercel` command."""

    def __init__(self, fail=None):
        self.calls: list[tuple[list[str], str | None, Path | None]] = []
        self.fail = fail

    def __call__(self, cmd, cwd=None, input=None, **kw):
        self.calls.append((list(cmd), input, cwd))
        r = type("R", (), {})()
        r.returncode, r.stdout, r.stderr = 0, "", ""
        if self.fail and self.fail in " ".join(cmd):
            r.returncode, r.stderr = 1, "boom"
        return r

    def env_adds(self) -> dict[str, tuple[list[str], str]]:
        return {c[3]: (c, i) for c, i, _ in self.calls if c[:3] == ["vercel", "env", "add"]}


@pytest.fixture(autouse=True)
def _vercel_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for k in [k for k in os.environ if k.startswith("REDBERRY_")]:      # the exporter's settings, not this test's
        monkeypatch.delenv(k)


def _connect(repo, *argv, run=None, ask=None, environ=None):
    args = cli.parser().parse_args(["connect", "--platform", "vercel", "--repo", str(repo), *argv])
    lines: list[str] = []
    if ask is None and environ is None:
        rc = cli.cmd_connect(args, out=lines.append, run=run)
    else:
        rc = vercel.cmd_connect(args, out=lines.append, run=run, ask=ask, environ=environ or {})
    return rc, "\n".join(lines)


def _env_file(tmp_path, text=GCP_OUTPUT) -> str:
    p = tmp_path / "install-output.txt"
    p.write_text(text)
    return str(p)


def test_connect_writes_the_app_and_sets_the_production_env(app, tmp_path):
    run = _Run()
    rc, out = _connect(app, "--yes", "--env-file", _env_file(tmp_path), run=run)
    assert rc == 0
    inst = (app / "instrumentation.ts").read_text()
    assert inst.startswith("// Written by `redberry connect --platform vercel`.")
    assert "import { registerOTel } from '@vercel/otel';" in inst
    assert ("const { redberryExporter, exporterOptionsFrom, validateAgentName } = await import('@redberrylabs/otel');"
            in inst)
    assert "serviceName: \"acme-support\"," in inst
    assert "attributes: { 'redberry.agent.name': validateAgentName(\"acme-support\") }," in inst
    assert "traceExporter: redberryExporter(exporterOptionsFrom({})),"  in inst
    assert "registerTelemetry(new OpenTelemetry({ runtimeContext: true }));" in inst
    assert "RedberryProcessor" not in inst and "SIGNING" not in inst
    deps = json.loads((app / "package.json").read_text())["dependencies"]
    assert deps["@redberrylabs/otel"] == "^0.6.0" and deps["@vercel/otel"] == "^2.1.3" and "@ai-sdk/otel" in deps
    assert {"@opentelemetry/api", "@opentelemetry/sdk-trace-base", "@opentelemetry/resources",
            "@opentelemetry/context-async-hooks"} <= set(deps)
    assert "temperature: 1," in (app / "lib" / "handler.ts").read_text()
    assert "temperature: 1" in (app / "app" / "api" / "(public)" / "chat" / "route.ts").read_text()
    adds = run.env_adds()
    assert list(adds) == ["REDBERRY_CLOUD", "REDBERRY_INGEST_URL", "REDBERRY_GCP_PROJECT_NUMBER",
                          "REDBERRY_GCP_POOL_ID", "REDBERRY_GCP_PROVIDER_ID", "REDBERRY_GCP_SERVICE_ACCOUNT"]
    cmd, value = adds["REDBERRY_GCP_PROVIDER_ID"]
    assert cmd == ["vercel", "env", "add", "REDBERRY_GCP_PROVIDER_ID", "production", "--force", "--yes"]
    assert value == "vercel-acme" and "--sensitive" not in cmd
    record = json.loads((app / ".redberry" / "vercel.json").read_text())
    assert record["agent_name"] == "acme-support" and record["cloud"] == "gcp"
    assert record["routes"] == ["/api/chat", "/api/refund"] and record["vercel_project"]["id"] == PROJECT_ID
    assert "Set 6 variables on the Vercel project's production environment" in out
    assert "Secure backend access" in out and "connect never deploys" in out
    # again: nothing more to change in the code; the variables are set again (--force)
    before = {p: p.read_text() for p in app.rglob("*.ts") if "node_modules" not in p.parts}
    rc, _ = _connect(app, "--yes", "--env-file", _env_file(tmp_path), run=_Run())
    assert rc == 0 and {p: p.read_text() for p in before} == before


def test_connect_asks_first_and_changes_nothing_when_not_told_yes(app, tmp_path):
    before = {p: p.read_text() for p in app.rglob("*") if p.is_file()}
    asked = []
    run = _Run()
    with pytest.raises(cli.CliError, match="nothing changed"):
        _connect(app, "--env-file", _env_file(tmp_path), run=run, ask=lambda q: asked.append(q) or False)
    assert asked == ["Apply these changes and set 6 REDBERRY_* variables on the Vercel project's production "
                     "environment?"]
    assert {p: p.read_text() for p in app.rglob("*") if p.is_file()} == before and not run.calls


def test_without_values_it_says_where_they_come_from(app):
    run = _Run()
    rc, out = _connect(app, "--yes", run=run)
    assert rc == 0 and not run.calls and (app / "instrumentation.ts").exists()
    assert "the install printed for --vercel-team" in out and "--env-file" in out


def test_values_from_the_environment_and_no_env_prints_them(app):
    environ = {"REDBERRY_API_URL": "https://api.example", **dict(
        line.split("=", 1) for line in AWS_OUTPUT.replace("==>", "").split() if line.startswith("REDBERRY_"))}
    run = _Run()
    rc, out = _connect(app, "--yes", "--no-env", run=run, environ=environ)
    assert rc == 0 and not run.calls
    assert "Not set on Vercel (--no-env)" in out
    assert "  REDBERRY_AWS_ROLE_ARN=arn:aws:iam::123456789012:role/redberry-vercel-sender" in out
    assert "REDBERRY_API_URL" not in out                         # the CLI's own setting, not the exporter's


def test_the_install_output_is_read_as_pasted(tmp_path):
    env = vercel.read_env_file(Path(_env_file(tmp_path, AWS_OUTPUT)))
    assert env == {"REDBERRY_CLOUD": "aws", "REDBERRY_INGEST_URL": "https://abc.lambda-url.eu-west-1.on.aws",
                   "REDBERRY_AWS_REGION": "eu-west-1",
                   "REDBERRY_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/redberry-vercel-sender"}
    assert vercel.read_env_file(Path(_env_file(tmp_path, GCP_OUTPUT)))["REDBERRY_GCP_POOL_ID"] == "redberry-vercel"
    azure = ("REDBERRY_CLOUD=azure\nREDBERRY_INGEST_URL=https://ingest.example\nREDBERRY_AZURE_TENANT_ID=t\n"
             "REDBERRY_AZURE_CLIENT_ID=c\nREDBERRY_AZURE_APP_ID_URI=api://x\nREDBERRY_GCP_POOL_ID=stray\n")
    values, ignored = vercel.env_values(vercel.read_env_file(Path(_env_file(tmp_path, azure))))
    assert list(values) == ["REDBERRY_CLOUD", "REDBERRY_INGEST_URL", "REDBERRY_AZURE_TENANT_ID",
                            "REDBERRY_AZURE_CLIENT_ID", "REDBERRY_AZURE_APP_ID_URI"]
    assert ignored == ["REDBERRY_GCP_POOL_ID"]


def test_an_incomplete_set_is_refused_before_anything_changes(app, tmp_path):
    with pytest.raises(cli.CliError, match="also prints REDBERRY_AWS_ROLE_ARN"):
        _connect(app, "--yes", "--env-file", _env_file(tmp_path, "REDBERRY_CLOUD=aws\n"
                 "REDBERRY_INGEST_URL=https://x\nREDBERRY_AWS_REGION=eu-west-1\n"), run=_Run())
    with pytest.raises(cli.CliError, match="REDBERRY_CLOUD is not set"):
        _connect(app, "--yes", "--env-file", _env_file(tmp_path, "REDBERRY_INGEST_URL=https://x\n"), run=_Run())
    with pytest.raises(cli.CliError, match="https:// URL"):
        _connect(app, "--yes", "--env-file", _env_file(tmp_path, GCP_OUTPUT.replace("https://redberry", "http://r")),
                 run=_Run())
    with pytest.raises(cli.CliError, match="--env-file .*missing"):
        _connect(app, "--yes", "--env-file", str(tmp_path / "missing"), run=_Run())
    assert not (app / "instrumentation.ts").exists()


def test_an_unlinked_app_prints_the_variables(app, tmp_path):
    (app / ".vercel" / "project.json").unlink()
    run = _Run()
    rc, out = _connect(app, "--yes", "--env-file", _env_file(tmp_path), run=run)
    assert rc == 0 and not run.calls and "the app is not linked: run vercel link" in out
    assert 'validateAgentName("acme-support")' in (app / "instrumentation.ts").read_text()   # package.json's name


def test_the_agent_name_is_kept_and_can_change(app):
    rc, _ = _connect(app, "--yes", "--name", "refunds", run=_Run())
    assert rc == 0 and 'validateAgentName("refunds")' in (app / "instrumentation.ts").read_text()
    rc, _ = _connect(app, "--yes", run=_Run())                    # the recorded name
    assert 'validateAgentName("refunds")' in (app / "instrumentation.ts").read_text()
    rc, _ = _connect(app, "--yes", "--name", "support", run=_Run())
    assert 'validateAgentName("support")' in (app / "instrumentation.ts").read_text()
    with pytest.raises(cli.CliError, match="1 to 64"):
        _connect(app, "--yes", "--name", "x" * 65, run=_Run())


def test_the_earlier_hasher_instrumentation_is_replaced(app):
    (app / "instrumentation.ts").write_text(
        "// Written by `redberry connect --platform vercel`.\n// @redberrylabs/otel hashes, signs and stores each "
        "AI SDK run.\nimport { registerOTel } from '@vercel/otel';\n\nexport async function register() {\n"
        "  const { RedberryProcessor } = await import('@redberrylabs/otel');\n"
        "  registerOTel({ serviceName: 'x', spanProcessors: ['auto', new RedberryProcessor()] });\n}\n")
    (app / ".redberry").mkdir()
    (app / ".redberry" / "vercel.json").write_text(json.dumps({"store": "redberry", "public_key": "ab" * 32}))
    pkg = json.loads((app / "package.json").read_text())
    pkg["dependencies"]["@redberrylabs/otel"] = "^0.2.0"
    (app / "package.json").write_text(json.dumps(pkg))
    rc, out = _connect(app, "--yes", run=_Run())
    inst = (app / "instrumentation.ts").read_text()
    assert rc == 0 and "RedberryProcessor" not in inst and "redberryExporter(exporterOptionsFrom({}))" in inst
    assert json.loads((app / "package.json").read_text())["dependencies"]["@redberrylabs/otel"] == "^0.6.0"
    assert "REDBERRY_SIGNING_KEY" in out and "remove its variables" in out


def test_someone_elses_instrumentation_is_not_overwritten(app):
    (app / "instrumentation.ts").write_text("export function register() { /* theirs */ }\n")
    rc, out = _connect(app, "--yes", run=_Run())
    assert rc == 0 and (app / "instrumentation.ts").read_text() == "export function register() { /* theirs */ }\n"
    assert "does not send to Redberry; merge this into its register()" in out


def test_a_src_app_layout_gets_src_instrumentation(app):
    (app / "src").mkdir()
    (app / "app").rename(app / "src" / "app")
    rc, _ = _connect(app, "--yes", run=_Run())
    assert rc == 0 and (app / "src" / "instrumentation.ts").exists() and not (app / "instrumentation.ts").exists()


def test_app_checks(app, tmp_path):
    pkg = json.loads((app / "package.json").read_text())
    pkg["dependencies"]["ai"] = "^6.0.0"
    (app / "package.json").write_text(json.dumps(pkg))
    rc, out = _connect(app, "--yes", run=_Run())
    assert rc == 0 and "AI SDK 6" in out
    del pkg["dependencies"]["next"]
    (app / "package.json").write_text(json.dumps(pkg))
    with pytest.raises(cli.CliError, match="not a Next.js app"):
        _connect(app, "--yes", run=_Run())
    with pytest.raises(cli.CliError, match="no package.json"):
        _connect(tmp_path / "nowhere", "--yes", run=_Run())
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "package.json").write_text(json.dumps({"dependencies": {"next": "16", "ai": "^7.0.0"}}))
    with pytest.raises(cli.CliError, match="nothing to connect"):
        _connect(empty, "--yes", run=_Run())


def test_a_failed_env_add_says_which_were_set(app, tmp_path):
    with pytest.raises(cli.CliError, match="REDBERRY_GCP_POOL_ID production` failed: boom. Set: REDBERRY_CLOUD, "
                                           "REDBERRY_INGEST_URL, REDBERRY_GCP_PROJECT_NUMBER"):
        _connect(app, "--yes", "--env-file", _env_file(tmp_path), run=_Run(fail="REDBERRY_GCP_POOL_ID"))


def test_vercel_flags_are_refused_on_other_platforms(tmp_path):
    with pytest.raises(cli.CliError, match="--no-env is for --platform vercel"):
        cli.cmd_connect(cli.parser().parse_args(["connect", "--platform", "aws-lambda", "--function", "f",
                                                 "--no-env"]), out=lambda _m: None)


def test_ts_tools_turn_zod_into_json_schema(app):
    tools = {t.name: t for t in ts.scan_tools(ts.source_files(app), app)}
    assert tools["lookup_customer"].parameters_schema == {
        "type": "object", "properties": {"email": {"type": "string"}, "customer_id": {"type": "string"}},
        "required": ["customer_id"], "additionalProperties": False}
    refund = tools["issue_refund"]
    assert refund.description == "Issue a refund. Irreversible."
    assert refund.parameters_schema["properties"] == {
        "amount": {"type": "number", "description": "Amount in GBP."},
        "reason": {"type": "string", "enum": ["damaged", "late"]},
        "tags": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}}
    weird = tools["weird"]
    assert weird.parameters_schema is None and weird.parameters_source == "mySchema"
    assert weird.description == "Uses a custom schema" and weird.file == "lib/tools.ts"


def test_model_calls_skip_node_modules_and_routes_follow_imports(app):
    calls = ts.scan_model_calls(ts.source_files(app))
    assert sorted((c.kind, c.file.name, c.temperature) for c in calls) == [
        ("generateText", "route.ts", None), ("streamText", "handler.ts", "0.2")]
    routes = vercel.agent_routes(app, calls)
    assert [(r.path, r.calls[0].file.name) for r in routes] == [("/api/chat", "route.ts"),
                                                                ("/api/refund", "handler.ts")]


def _edit(body: str) -> tuple[str, str]:
    import tempfile
    f = Path(tempfile.mkdtemp()) / "x.ts"
    f.write_text("import { streamText } from 'ai';\n" + body)
    (call,) = ts.scan_model_calls([f])
    new, what = ts.set_temperature_one(f.read_text(), call)
    return new.split("\n", 1)[1], what


def test_temperature_edits():
    assert _edit("streamText({ model, temperature: 0.7 })") == (
        "streamText({ model, temperature: 1 })", "replaced 0.7")
    assert _edit("streamText({ model, temperature: 1.0 })")[1] == "kept"
    assert _edit("streamText({ model, temperature: t })") == ("streamText({ model, temperature: t })", "variable")
    assert _edit("streamText({ model })") == ("streamText({ temperature: 1, model })", "inserted")
    assert _edit("const a = new ToolLoopAgent({\n  model,\n});") == (
        "const a = new ToolLoopAgent({\n  temperature: 1,\n  model,\n});", "inserted")


def test_temperature_edit_offsets_are_the_files(tmp_path):
    f = tmp_path / "x.ts"
    f.write_text("import { streamText } from 'ai';\nstreamText({ model, temperature: 0.7 })\n")
    (call,) = ts.scan_model_calls([f])
    new, what = ts.set_temperature_one(f.read_text(), call)
    assert what == "replaced 0.7" and new.endswith("streamText({ model, temperature: 1 })\n")




def test_connect_raises_an_outdated_package_range():
    # ^0.5.0 never installs 0.6.0: a connect that needs the newer package raises the range.
    wanted = vercel.DEPENDENCIES
    assert ed.outdated({"@redberrylabs/otel": "^0.5.0"}, wanted) == {"@redberrylabs/otel": "^0.6.0"}
    assert ed.outdated({"@redberrylabs/otel": "^0.6.1"}, wanted) == {}
    assert ed.outdated({"@redberrylabs/otel": "latest"}, wanted) == {}
