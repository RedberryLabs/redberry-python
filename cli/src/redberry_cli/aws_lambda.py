"""``redberry connect --platform aws-lambda``: an agent in an AWS Lambda function.

The function sends its own spans to the customer's Redberry installation: ``redberry_sdk.init()`` in a Python
function, ``init()`` from ``@redberrylabs/otel/init`` (0.5.0 or later) in a Node.js one. Neither needs a key or
a variable: each reads ``ingest.json`` from the installation's bucket in the function's account and region
and signs each export with the function's own credentials. connect only wires the code:

1. reads the function on AWS (``aws lambda get-function-configuration``): its runtime and its handler
   (``Handler``: ``file.export``, e.g. ``index.handler`` or ``app.lambda_handler``). A container-image
   function has no handler to read: connect changes nothing and prints what to add to the image;
2. reads the code here (``--repo``): Node.js (``package.json``) or Python (``requirements.txt``, or the
   handler's module), and finds the handler's file;
3. one agent per function, named by ``--name`` (default: the function's name);
4. after showing the diff and asking (``--yes`` applies without asking), edits the code:

   * Node.js: ``@redberrylabs/otel`` and its OpenTelemetry peers in ``package.json``, ``init({ agent })``
     (and the AI SDK's OpenTelemetry integration) once at module load, and the exported handler wrapped
     so it sends its spans before it returns (Lambda freezes the process once it has);
   * Python: ``redberry-sdk[providers]`` in ``requirements.txt``, ``redberry_sdk.init(agent=…)`` once at
     module load, and ``@redberry_sdk.flush_after`` on the handler;
   * ``temperature: 1`` on each model call (pricing needs exactly 1);

   and writes ``.redberry/aws-lambda.json`` (what was found; no secret).

A function an earlier connect wired with the in-process hasher (``redberry_sdk.hasher``, ``withRedberry``) is
moved to the form above; the ``REDBERRY_*`` variables that wiring needed may be removed from its environment.
connect never deploys: the last step is yours (``sam deploy``, ``cdk deploy``, or a zip and ``aws lambda
update-function-code``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from redberry_cli import _edits as ed
from redberry_cli import _ts_tools as ts
from redberry_cli import aws_setup
from redberry_cli.main import CliError

PLATFORM = "aws-lambda"
PROJECT_FILE = Path(".redberry") / "aws-lambda.json"
NODE_PACKAGE = ed.NODE_PACKAGE
NODE_PACKAGE_RANGE = ed.NODE_PACKAGE_RANGE
PY_REQUIREMENT = ed.PY_REQUIREMENT
_NODE_SUFFIXES = (".mjs", ".js", ".cjs", ".ts", ".mts", ".cts")
_BUILD_DIRS = ("dist", "build", "out", ".build", "lib")
#: What the Node.js wiring imports beside the package and its peers. ``ai`` only when the function has none.
NODE_DEPENDENCIES = {**ed.NODE_DEPENDENCIES, "@ai-sdk/otel": "^1.0.148"}
AI_SDK = {"ai": f"^{ed.AI_SDK_MAJOR}.0.0"}
#: The wrapper connect puts around the exported handler (defined in the set-up block it adds).
WRAPPER = "redberryFlushAfter"


@dataclass
class Handler:
    """Where the function's handler is: ``Handler`` = ``<path>.<export>``."""

    spec: str
    file: Path | None
    export: str


# ── the function on AWS ─────────────────────────────────────────────────────────────────────


def _region(a, run) -> str:
    value = a.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not value:
        r = aws_setup.aws(run, ["aws", "configure", "get", "region"])
        value = (getattr(r, "stdout", "") or "").strip() if r.returncode == 0 else ""
    try:
        return aws_setup.region(value)
    except aws_setup.SetupError:
        raise CliError("--platform aws-lambda needs the function's region: --region (e.g. eu-west-1)") from None


def function_config(a, run) -> dict:
    """``aws lambda get-function-configuration``, and {name, region, account_id} from its ARN."""
    reg = _region(a, run)
    cmd = ["aws", "lambda", "get-function-configuration", "--function-name", a.function, "--region", reg,
           "--output", "json"]
    try:
        r = aws_setup.aws(run, cmd)
    except aws_setup.SetupError as e:
        raise CliError(str(e)) from None
    if r.returncode != 0:
        raise CliError(f"cannot read the Lambda function {a.function} in {reg} (aws lambda get-function-configuration): "
                       f"{(getattr(r, 'stderr', '') or '').strip() or 'is the aws CLI signed in?'}")
    try:
        cfg = json.loads(getattr(r, "stdout", "") or "{}")
    except ValueError:
        cfg = {}
    arn = str(cfg.get("FunctionArn") or "")
    parts = arn.split(":")
    if len(parts) < 7 or parts[2] != "lambda" or not re.fullmatch(r"\d{12}", parts[4]):
        raise CliError(f"aws lambda get-function-configuration gave no function ARN for {a.function}")
    cfg["_function"] = {"name": cfg.get("FunctionName") or a.function, "region": parts[3], "account_id": parts[4]}
    return cfg


def iac_tools(repo: Path, tags: dict) -> list[str]:
    """What else deploys this function (and so sets its environment): from the repo's files and the
    function's tags."""
    found = []
    if (repo / "template.yaml").exists() or (repo / "template.yml").exists() or (repo / "samconfig.toml").exists():
        found.append("SAM")
    if (repo / "cdk.json").exists():
        found.append("CDK")
    if any((repo / n).exists() for n in ("serverless.yml", "serverless.yaml", "serverless.ts")):
        found.append("the Serverless Framework")
    if any(repo.glob("*.tf")) or any(repo.glob("*/*.tf")):
        found.append("Terraform")
    if not found and any(k.startswith("aws:cloudformation:") for k in tags):
        found.append("CloudFormation")
    return found


def function_tags(cfg: dict, run) -> dict:
    r = aws_setup.aws(run, ["aws", "lambda", "list-tags", "--resource", cfg["FunctionArn"], "--region",
                            cfg["_function"]["region"], "--output", "json"])
    try:
        return dict(json.loads(getattr(r, "stdout", "") or "{}").get("Tags") or {}) if r.returncode == 0 else {}
    except (ValueError, AttributeError):
        return {}


# ── the code here ───────────────────────────────────────────────────────────────────────────


def detect_runtime(repo: Path, cfg: dict) -> tuple[str, list[str]]:
    """``node`` or ``python``: the function's runtime on AWS when it says (a zip function), checked against
    the code here; a container image's from the code here."""
    if not repo.is_dir():
        raise CliError(f"--repo {repo}: no such directory")
    notes: list[str] = []
    runtime = str(cfg.get("Runtime") or "")
    on_aws = "node" if runtime.startswith("nodejs") else "python" if runtime.startswith("python") else None
    if runtime and on_aws is None:
        raise CliError(f"the function runs {runtime}: connect wires Node.js and Python functions")
    node_here = (repo / "package.json").exists()
    python_here = (repo / "requirements.txt").exists() or (repo / "pyproject.toml").exists() or bool(
        cfg.get("Handler") and _python_module(repo, str(cfg["Handler"]).rsplit(".", 1)[0]))
    if on_aws == "node" and not node_here:
        raise CliError(f"{a_name(cfg)} runs {runtime}, but {repo} has no package.json: run connect in the function's "
                       "folder, or --repo")
    if on_aws == "python" and not python_here:
        raise CliError(f"{a_name(cfg)} runs {runtime}, but {repo} has no requirements.txt and no handler module "
                       f"{cfg.get('Handler')}: run connect in the function's folder, or --repo")
    if on_aws:
        return on_aws, notes
    if node_here and python_here:
        raise CliError(f"{repo} holds both a package.json and Python code, and the function (a container image) does "
                       "not say which it runs: point --repo at one of them")
    if not (node_here or python_here):
        raise CliError(f"{repo} has no package.json or requirements.txt: run connect in the function's folder, or --repo")
    return ("node" if node_here else "python"), notes


def a_name(cfg: dict) -> str:
    return str((cfg.get("_function") or {}).get("name") or cfg.get("FunctionName") or "the function")


def _python_module(repo: Path, module: str) -> Path | None:
    rel = Path(*module.replace("/", ".").split("."))
    for base in (repo, repo / "src"):
        for p in (base / rel.with_suffix(".py"), base / rel / "__init__.py"):
            if p.is_file():
                return p
    return None


def _node_file(repo: Path, path: str) -> Path | None:
    stem = repo / path
    for suffix in _NODE_SUFFIXES:
        if stem.with_name(stem.name + suffix).is_file():
            return stem.with_name(stem.name + suffix)
    # A compiled handler (dist/index.handler): its TypeScript source.
    parts = Path(path).parts
    rest = Path(*parts[1:]) if len(parts) > 1 and parts[0] in _BUILD_DIRS else None
    for base in ([repo / "src" / rest, repo / rest] if rest else [repo / "src" / path]):
        for suffix in (".ts", ".mts", ".cts", ".js", ".mjs"):
            if base.with_name(base.name + suffix).is_file():
                return base.with_name(base.name + suffix)
    return None


def find_handler(repo: Path, runtime: str, spec: str | None) -> Handler | None:
    """The handler's file and export from Lambda's ``Handler`` (``index.handler``, ``src/app.main``,
    ``app.lambda_handler``, ``pkg.mod.handler``)."""
    if not spec or "." not in spec:
        return None
    path, export = spec.rsplit(".", 1)
    f = _node_file(repo, path) if runtime == "node" else _python_module(repo, path)
    return Handler(spec, f.resolve() if f else None, export)


# ── the edits: Node.js ──────────────────────────────────────────────────────────────────────


_CONTINUES = re.compile(r"(=>|\.|\?|:|&&|\|\||\+|-(?!-)|\*|/(?![/*])|,)")
_OPEN_END = re.compile(r"(=>|[=(\[{,?:+\-*/&|.])\s*$")


def _expression_end(text: str, i: int) -> int:
    """The end of the expression starting at ``text[i]`` (an assignment's right-hand side): before the ``;``
    at bracket depth 0, or a newline at depth 0 that neither the line before nor the line after continues."""
    n = len(text)
    start = i
    while i < n:
        c = text[i]
        if c in "'\"`":
            i = ts._skip_string(text, i)
            continue
        if c == "/" and text[i + 1:i + 2] == "/":
            nl = text.find("\n", i)
            return n if nl < 0 else nl
        if c in "([{":
            i = ts.match_bracket(text, i) + 1
            continue
        if c == ";":
            return i
        if c == "\n":
            before, after = text[start:i], text[i:].lstrip()
            if before.strip() and not _OPEN_END.search(before) and not _CONTINUES.match(after):
                return i
        i += 1
    return n


_ESM_CONST = r"export\s+(?:const|let|var)\s+{name}\s*(?::[^=]+)?=(?!=)\s*"
_CJS_EXPORT = r"(?<![\w$.])(?:module\s*\.\s*)?exports\s*\.\s*{name}\s*=(?!=)\s*"
_ESM_FUNCTION = r"export\s+(async\s+)?function\s*\*?\s*{name}\s*\("



def _unwrap_old(expr: str) -> str | None:
    """The handler inside the earlier connect's ``withRedberry(handler, { agentId: … })``, or None."""
    m = re.fullmatch(r"withRedberry\(\s*(.*?)\s*,\s*\{[^{}]*\}\s*\)", expr, re.S)
    return m.group(1) if m else None


def wrap_node_handler(text: str, export: str) -> tuple[str, str | None]:
    """``text`` with the exported handler wrapped in ``redberryFlushAfter``, and how (None: no form it knows;
    "already": it is wrapped). The earlier connect's ``withRedberry(handler, …)`` becomes the new wrapper."""
    name = re.escape(export)
    wrapped = f"redberry{export[:1].upper()}{export[1:]}"
    # An exported function, wrapped by an earlier connect: re-exported under its name from the wrapper.
    old = re.search(rf"const {re.escape(wrapped)} = withRedberry\({name}, \{{[^{{}}]*\}}\);", text)
    if old:
        return text[:old.start()] + f"const {wrapped} = {WRAPPER}({export});" + text[old.end():], "export function"
    if re.search(rf"const {re.escape(wrapped)} = {WRAPPER}\({name}\);", text):
        return text, "already"
    for pattern, how in ((_ESM_CONST, "export const"), (_CJS_EXPORT, "exports")):
        m = re.search(pattern.format(name=name), text)
        if m:
            start = m.end()
            end = _expression_end(text, start)
            expr = text[start:end].rstrip()
            if expr.startswith(f"{WRAPPER}("):
                return text, "already"
            inner = _unwrap_old(expr)
            return text[:start] + f"{WRAPPER}({inner if inner is not None else expr})" + text[start + len(expr):], how
    m = re.search(_ESM_FUNCTION.format(name=name), text)
    if m:
        brace = text.find("{", ts.match_bracket(text, m.end() - 1))
        if brace < 0:
            return text, None
        close = ts.match_bracket(text, brace)
        tail = (f"\n\n// Redberry: the handler Lambda calls is the wrapped one.\n"
                f"const {wrapped} = {WRAPPER}({export});\nexport {{ {wrapped} as {export} }};\n")
        new = text[:m.start()] + re.sub(r"^export\s+", "", text[m.start():close + 1]) + tail + text[close + 1:].lstrip("\n")
        return new, "export function"
    return text, None


NODE_SETUP = """\
// Redberry (redberry connect --platform aws-lambda): init() sends the function's spans to the Redberry
// installation, which it finds from the function's own credentials; there is nothing to configure.
{imports}
const redberry = init({{ agent: {agent} }});
{ai_register}// Lambda freezes the process once a handler returns: send the invocation's spans first.
{wrapper}"""
_WRAPPER_JS = f"""\
const {WRAPPER} = (handler) => async (...args) => {{
  try {{
    return await handler(...args);
  }} finally {{
    await redberry.provider.forceFlush();
  }}
}};
"""
_WRAPPER_TS = f"""\
const {WRAPPER} = <A extends unknown[], R>(handler: (...args: A) => R) => async (...args: A): Promise<Awaited<R>> => {{
  try {{
    return await handler(...args);
  }} finally {{
    await redberry.provider.forceFlush();
  }}
}};
"""
AI_REGISTER = "// AI SDK 7 emits no spans until an integration is registered.\nregisterTelemetry(new OpenTelemetry({ runtimeContext: true }));\n"
#: The earlier connect's set-up block (the in-process hasher), removed when the function is connected again.
_OLD_SETUP = (re.compile(r"(?m)^// Redberry \(redberry connect --platform aws-lambda\): hashes, signs and stores[^\n]*\n"
                         r"// wrapped in withRedberry\.[^\n]*\n"),
              re.compile(r"(?m)^(?:import \{ registerRedberry, withRedberry \} from '@redberrylabs/otel/node';|"
                         r"const \{ registerRedberry, withRedberry \} = require\('@redberrylabs/otel/node'\);)[ \t]*\n\n?"),
              re.compile(r"(?m)^registerRedberry\(\{[^\n]*\}\);[ \t]*\n"))


def _is_cjs(text: str, path: Path, pkg: dict) -> bool:
    if path.suffix in (".cjs", ".cts"):
        return True
    if path.suffix in (".mjs", ".mts", ".ts", ".tsx"):
        return False
    return pkg.get("type") != "module" and "require(" in text and not re.search(r"(?m)^import\b", text)


def _top_of_file(text: str) -> int:
    """After a shebang and a leading 'use strict', where the set-up goes (before every other import)."""
    m = re.match(r"(?:#![^\n]*\n)?(?:\s*(?:'use strict'|\"use strict\");?[^\n]*\n)?", text)
    return m.end() if m else 0


def node_setup(agent: str, cjs: bool, typescript: bool, ai: bool) -> str:
    if cjs:
        imports = ("const { registerTelemetry } = require('ai');\nconst { OpenTelemetry } = require('@ai-sdk/otel');\n"
                   if ai else "") + "const { init } = require('@redberrylabs/otel/init');\n"
    else:
        imports = ("import { registerTelemetry } from 'ai';\nimport { OpenTelemetry } from '@ai-sdk/otel';\n"
                   if ai else "") + "import { init } from '@redberrylabs/otel/init';\n"
    return NODE_SETUP.format(imports=imports, agent=json.dumps(agent), ai_register=AI_REGISTER if ai else "",
                             wrapper=_WRAPPER_TS if typescript else _WRAPPER_JS)


def _node_edits(repo: Path, handler: Handler, agent: str, notes: list[str]) -> list[ed.Edit]:
    edits: list[ed.Edit] = []
    pkg_path = repo / "package.json"
    pkg_text = pkg_path.read_text(encoding="utf-8")
    try:
        pkg = json.loads(pkg_text)
    except ValueError as e:
        raise CliError(f"{pkg_path} is not valid JSON ({e})") from None
    present = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
    wanted = {**NODE_DEPENDENCIES, **({} if "ai" in present else AI_SDK)}
    missing = {k: v for k, v in wanted.items() if k not in present} | ed.outdated(present, wanted)
    if "ai" not in present:
        notes.append(f"Warning: the function does not depend on the Vercel AI SDK (`ai`): its spans are what Redberry "
                     f"records; added `ai` {AI_SDK['ai']}, but the model calls must go through it.")
    elif (ed.major(present["ai"]) or ed.AI_SDK_MAJOR) < ed.AI_SDK_MAJOR:
        notes.append(f"Warning: the function uses AI SDK {ed.major(present['ai'])} (`ai: {present['ai']}`); the set-up "
                     f"registers AI SDK {ed.AI_SDK_MAJOR}'s telemetry. Upgrade `ai` before deploying.")
    if missing:
        edits.append(ed.package_json_edit(pkg_path, pkg_text, pkg, missing))

    files = ts.source_files(repo)
    by_file = ed.calls_by_file(ts.scan_model_calls(files))
    calls = ed.reachable_calls(handler.file, by_file, ed.alias_roots(repo)) if handler.file else []
    temps = ed.temperature_edits(repo, calls, notes)
    if handler.file is not None:
        f = handler.file
        before = f.read_text(encoding="utf-8")
        text = temps[f][1] if f in temps else before
        whys = [temps[f][2]] if f in temps else []
        migrated = text
        for rx in _OLD_SETUP:
            migrated = rx.sub("", migrated)
        if migrated != text:
            whys.append("replace the earlier registerRedberry() set-up")
            text = migrated
        text, how = wrap_node_handler(text, handler.export)
        if how is None:
            notes.append(f"Warning: {f.relative_to(repo)} does not export `{handler.export}` in a form connect can "
                         f"wrap (export const / export function / exports.{handler.export} =): wrap it yourself as "
                         f"{WRAPPER}({handler.export}), defined as in the block connect adds, so its spans are sent "
                         "before it returns.")
        elif how != "already":
            whys.insert(0, f"send the spans of the exported {handler.export} before it returns")
        if "@redberrylabs/otel/init" not in text:
            cjs = _is_cjs(text, f, pkg)
            ai = ("ai" in present or "ai" in missing) and "registerTelemetry" not in text
            setup = node_setup(agent, cjs, f.suffix in (".ts", ".mts", ".cts"), ai)
            at = _top_of_file(text)
            text = text[:at] + setup + "\n" + text[at:]
            whys.insert(1 if how not in (None, "already") else 0, "init() at module load")
            if not cjs and pkg.get("type") != "module" and f.suffix not in (".mjs", ".mts", ".ts"):
                notes.append(f"Warning: {f.relative_to(repo)} now uses ES module imports: make the package ESM "
                             "(\"type\": \"module\") or rename it .mjs, and set the function's Handler to match.")
        if text != before:
            edits.append(ed.Edit(f, before, text, "; ".join(whys)))
    for f, (before, after, why) in temps.items():
        if f != handler.file:
            edits.append(ed.Edit(f, before, after, why))
    return edits


# ── the edits: Python ───────────────────────────────────────────────────────────────────────


def _python_edits(repo: Path, handler: Handler, agent: str, notes: list[str]) -> list[ed.Edit]:
    from redberry_cli import azure_functions as azf      # the same temperature pass and file walk

    edits: list[ed.Edit] = []
    req = ed.py_requirement_edit(repo / "requirements.txt")
    if req is not None:
        edits.append(req)
    texts, temp_notes = azf._python_temperatures(repo, azf._py_files(repo))
    notes += temp_notes
    whys: dict[Path, list[str]] = {f.resolve(): ["temperature=1.0 on its model calls"] for f in texts}
    texts = {f.resolve(): t for f, t in texts.items()}
    f = handler.file
    if f is not None:
        import ast

        text = texts.get(f) or f.read_text(encoding="utf-8")
        tree = ast.parse(text)
        is_def = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == handler.export
                     for n in tree.body)
        is_assign = any(isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                        and n.targets[0].id == handler.export for n in tree.body)
        if not (is_def or is_assign):
            notes.append(f"Warning: {f.relative_to(repo)} has no top-level `def {handler.export}` (or `{handler.export} = "
                         f"…`) to decorate: add @redberry_sdk.flush_after to the handler yourself.")
        new, why = ed.py_wire(text, agent, {handler.export} if is_def else set(), True, str(f.relative_to(repo)),
                              notes, assigned={handler.export} if is_assign else set())
        whys.setdefault(f, [])[:0] = why
        texts[f] = new
    for path, new in sorted(texts.items()):
        before = path.read_text(encoding="utf-8")
        if new != before:
            edits.append(ed.Edit(path, before, new, "; ".join(whys.get(path, []))))
    return edits


def plan_edits(repo: Path, runtime: str, handler: Handler | None, agent: str) -> tuple[list[ed.Edit], list[str]]:
    notes: list[str] = []
    if handler is None or handler.file is None:
        return [], notes
    if runtime == "node":
        return _node_edits(repo, handler, agent, notes), notes
    return _python_edits(repo, handler, agent, notes), notes


# ── the command ─────────────────────────────────────────────────────────────────────────────


def _record(repo: Path) -> dict:
    try:
        return json.loads((repo / PROJECT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _same_function(record: dict, fn: dict) -> bool:
    return (record.get("aws_lambda_function") or {}) == fn


def _agent_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise CliError("--name: 1 to 64 printable characters")
    return name


def deploy_hint(repo: Path, runtime: str, cfg: dict, tools: list[str]) -> list[str]:
    fn = cfg["_function"]
    if cfg.get("PackageType") == "Image":
        return [("rebuild and push the image, then  aws lambda update-function-code --function-name "
                 f"{fn['name']} --region {fn['region']} --image-uri "
                 f"<account>.dkr.ecr.{fn['region']}.amazonaws.com/<repo>:<tag>")]
    if "SAM" in tools:
        return ["sam build", "sam deploy"]
    if "CDK" in tools:
        return ["cdk deploy"]
    if "the Serverless Framework" in tools:
        return ["serverless deploy"]
    if "Terraform" in tools:
        return ["terraform apply"]
    update = (f"aws lambda update-function-code --function-name {fn['name']} --region {fn['region']} "
              "--zip-file fileb://function.zip")
    if runtime == "node":
        install = ("pnpm install --prod" if (repo / "pnpm-lock.yaml").exists() else
                   "yarn install --production" if (repo / "yarn.lock").exists() else "npm install --omit=dev")
        build = ["npm run build"] if '"build"' in (repo / "package.json").read_text(encoding="utf-8") else []
        return [*build, install, "zip -r function.zip . -x '.git/*'", update]
    arch = "aarch64" if "arm64" in (cfg.get("Architectures") or []) else "x86_64"
    return [(f"pip install -r requirements.txt --target package --platform manylinux2014_{arch} --only-binary=:all: "
             f"--python-version {str(cfg.get('Runtime') or 'python3.12').removeprefix('python')}"),
            "(cd package && zip -r ../function.zip .) && zip -r function.zip . -x 'package/*' '.git/*'", update]




def cmd_connect(a, out=print, run=None, ask=None) -> int:
    run = run or subprocess.run
    ask = ask or ed.asker(a)
    say = (lambda _m: None) if a.json else out
    if not a.function:
        raise CliError("--platform aws-lambda needs --function <name> (and --region)")
    repo = Path(a.repo).resolve()
    record = _record(repo)
    cfg = function_config(a, run)
    fn = cfg["_function"]
    runtime, notes = detect_runtime(repo, cfg)
    image = cfg.get("PackageType") == "Image"
    handler = None if image else find_handler(repo, runtime, cfg.get("Handler"))
    name = _agent_name(a.name or (record.get("agent_name") if _same_function(record, fn) else None) or fn["name"])
    if image:
        notes.append(f"Warning: {fn['name']} is a container image: connect cannot see its handler, so it changes no "
                     f"code. In the image: Node.js  init({{ agent: {json.dumps(name)} }})  from "
                     f"'@redberrylabs/otel/init' ({NODE_PACKAGE_RANGE}) at module load, and flush its provider before "
                     f"the handler returns; Python  redberry_sdk.init(agent={json.dumps(name)})  at module load and "
                     f"@redberry_sdk.flush_after on the handler ({PY_REQUIREMENT}).")
    elif handler is None or handler.file is None:
        raise CliError(f"cannot find the handler {cfg.get('Handler')!r} of {fn['name']} in {repo}"
                       + ("" if handler is None else f" (no {handler.spec.rsplit('.', 1)[0]} source file)")
                       + ": run connect in the function's folder, or --repo")
    tools = iac_tools(repo, function_tags(cfg, run))

    say(f"Lambda function: {fn['name']} ({fn['region']}, account {fn['account_id']}), {runtime}"
        + ("" if image else f", handler {cfg.get('Handler')} in {handler.file.relative_to(repo)}"))
    say(f"  agent {name}")
    edits, edit_notes = plan_edits(repo, runtime, handler, name)
    notes += edit_notes
    if edits:
        say("Changes to the function's code:")
        for e in edits:
            say(f"  {e.path.relative_to(repo)}: {e.why}")
        if not a.json:
            out(ed.diff(edits, repo).rstrip())
        if not a.yes and not ask("Apply these changes?"):
            raise CliError("nothing changed" + ("; --yes applies without asking"
                                                if a.json or not sys.stdin.isatty() else ""))
    for e in edits:
        e.path.parent.mkdir(parents=True, exist_ok=True)
        e.path.write_text(e.after, encoding="utf-8")
    description = {
        "platform": PLATFORM, "aws_lambda_function": fn, "runtime": runtime, "handler": cfg.get("Handler"),
        "package_type": cfg.get("PackageType") or "Zip", "agent_name": name,
        **({"handler_file": str(handler.file.relative_to(repo))} if handler and handler.file else {}),
    }
    (repo / PROJECT_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / PROJECT_FILE).write_text(json.dumps(description, indent=2) + "\n", encoding="utf-8")
    old = sorted(k for k in ((cfg.get("Environment") or {}).get("Variables") or {}) if k in _HASHER_VARIABLES)
    if old:
        notes.append(f"{fn['name']}'s environment still has the earlier in-process wiring's variables "
                     f"({', '.join(old)}): nothing reads them now, and REDBERRY_SIGNING_KEY and REDBERRY_PROJECT_TOKEN "
                     "are secrets, so remove them (where the function's environment is defined).")
    notes.append(f"init() finds the Redberry installation in {fn['account_id']}/{fn['region']} with the function's own "
                 "role, which needs no policy: the installation grants it. REDBERRY_INGEST_URL overrides that.")
    nxt = deploy_hint(repo, runtime, cfg, tools)
    result = {
        "platform": PLATFORM, "aws_lambda_function": fn, "runtime": runtime, "agent_name": name,
        "edited": [str(e.path.relative_to(repo)) for e in edits], "iac": tools, "notes": notes, "next": nxt,
    }
    if a.json:
        out(json.dumps(result, indent=2))
        return 0
    for n in notes:
        out(n)
    if edits:
        out(f"Edited {', '.join(result['edited'])}; wrote {PROJECT_FILE}")
    elif not image:
        out(f"{fn['name']} is already wired for Redberry; wrote {PROJECT_FILE}")
    out(f"Next: {' then '.join(nxt)}. connect never deploys.")
    return 0


#: The variables the earlier in-process wiring set on a function; the SDK reads none of them.
_HASHER_VARIABLES = frozenset({"REDBERRY_PROJECT_TOKEN", "REDBERRY_URL", "REDBERRY_SIGNING_KEY", "REDBERRY_KEY_ID",
                               "REDBERRY_STORE", "REDBERRY_S3_BUCKET", "REDBERRY_S3_REGION", "REDBERRY_DEADLINE_MS"})


__all__ = ["Handler", "cmd_connect", "deploy_hint", "detect_runtime", "find_handler", "function_config",
           "iac_tools", "plan_edits", "wrap_node_handler"]
