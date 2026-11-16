"""``redberry connect --platform azure-functions``: agents in an Azure function app.

The app sends its own spans to the customer's Redberry installation: ``redberry_sdk.init()`` in a Python app,
``init()`` from ``@redberrylabs/otel/init`` in a Node.js one. Neither needs a key or a setting: each takes a
token from the app's managed identity and asks Redberry for the ingest URL of the installation that
registered that identity (chosen when Redberry was installed). connect only wires the code:

1. reads the app: Node.js (a ``package.json`` with ``@azure/functions`` 4, the v4 programming model) or
   Python (``function_app.py``, or ``azure-functions`` in ``requirements.txt``: the v2 programming
   model). The older models (a ``function.json`` per function) are not supported;
2. finds the function app on Azure (``az functionapp show``), to check it is there and what it runs;
3. finds the HTTP-triggered functions (Node: ``app.http('name', { handler })`` and ``app.get`` /
   ``app.post`` …; Python: ``@app.route(...)``) and the model calls each one reaches, statically:
   the AI SDK's calls through ``redberry_cli._ts_tools`` (following local imports), and the OpenAI and
   Anthropic SDKs' calls in Python (``redberry_cli.instrument``). The app is one agent: ``init()`` names
   every span the process sends. ``--name`` names it (default: the function app's name), and
   ``--name agent=function`` picks a function whose calls the static reader could not see;
4. after showing the diff and asking (``--yes`` applies without asking), edits the app:

   * Node.js: ``@redberrylabs/otel`` (^0.5.0) and its OpenTelemetry peers in ``package.json``, and a set-up
     module (``src/redberry.<ext>``) that calls ``init({ agent })`` and registers the AI SDK's
     OpenTelemetry integration, imported first by each agent's function file;
   * Python: ``redberry-sdk[providers]`` in ``requirements.txt``, ``redberry_sdk.init(agent=…)`` once at
     module load, and ``@redberry_sdk.flush_after`` on each agent's function, under its trigger decorators;
   * ``temperature: 1`` on each model call (pricing needs exactly 1);

   and writes ``.redberry/azure-functions.json`` (what was found; no secret).

An app an earlier connect wired with the in-process hasher (``redberry_sdk.hasher``, ``withRedberry``) is moved
to the form above. It never deploys: the last step is yours (``func azure functionapp publish <app>``).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from redberry_cli import _edits as ed
from redberry_cli import _ts_tools as ts
from redberry_cli import _cloud, instrument
from redberry_cli.main import CliError

PLATFORM = "azure-functions"
PROJECT_FILE = Path(".redberry") / "azure-functions.json"
NODE_PACKAGE = ed.NODE_PACKAGE
PY_REQUIREMENT = ed.PY_REQUIREMENT
FUNCTIONS_MAJOR = 4
_PY_SKIP = (".venv", "venv", "node_modules", "site-packages", "__pycache__", "tests", ".python_packages")


@dataclass
class Function:
    """An HTTP-triggered function: its name on Azure, where it is registered, and the model calls it reaches."""

    name: str
    file: Path
    line: int
    calls: list = field(default_factory=list)       # ts.ModelCall (Node) or (file, line) (Python)


# ── the app ─────────────────────────────────────────────────────────────────────────────────


def _deps(pkg: dict) -> dict:
    return {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}


def detect_runtime(repo: Path) -> tuple[str, list[str]]:
    """``node`` or ``python``, and warnings. Refuses what is not a function app on a supported model."""
    if not repo.is_dir():
        raise CliError(f"--repo {repo}: no such directory")
    notes: list[str] = []
    node = python = False
    pkg_path = repo / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise CliError(f"{pkg_path} is not valid JSON ({e})") from None
        spec = _deps(pkg).get("@azure/functions")
        if spec is not None:
            major = ed.major(spec)
            if major is not None and major < FUNCTIONS_MAJOR:
                raise CliError(f"the app uses @azure/functions {spec}: connect needs the v4 programming model "
                               f"(@azure/functions ^{FUNCTIONS_MAJOR}, functions registered with app.http)")
            node = True
    reqs = repo / "requirements.txt"
    if (repo / "function_app.py").exists() or (
            reqs.exists() and re.search(r"(?im)^\s*azure-functions\b", reqs.read_text(encoding="utf-8"))):
        python = True
    if node and python:
        raise CliError(f"{repo} holds both a Node.js and a Python function app: point --repo at one of them")
    if not (node or python):
        raise CliError(f"{repo} is not an Azure Functions app: no package.json with @azure/functions and no "
                       "function_app.py (run connect in the function app's folder, or --repo)")
    if any(repo.glob("*/function.json")):
        notes.append("Warning: this app has function.json files (the older programming model); only functions "
                     "registered in code (Node.js v4 app.http, Python v2 @app.route) are connected.")
    return ("node" if node else "python"), notes


def function_app(a, run) -> dict[str, str]:
    """{name, resource_group, subscription_id} of the function app on Azure, and its runtime if Azure says."""
    cmd = ["az", "functionapp", "show", "--name", a.app, "--resource-group", a.resource_group,
           *(["--subscription", a.subscription] if a.subscription else []), "-o", "json"]
    try:
        r = _cloud.az(run, cmd)
    except _cloud.SetupError as e:
        raise CliError(str(e)) from None
    if r.returncode != 0:
        raise CliError(f"cannot find the function app {a.app} in {a.resource_group} (az functionapp show): "
                       f"{(getattr(r, 'stderr', '') or '').strip() or 'is az signed in (az login)?'}")
    try:
        app = json.loads(getattr(r, "stdout", "") or "{}")
    except ValueError:
        app = {}
    m = re.match(r"/subscriptions/([^/]+)/", app.get("id") or "")
    subscription = a.subscription if a.subscription and re.fullmatch(r"[0-9a-f-]{36}", a.subscription) else None
    subscription = (m.group(1) if m else None) or subscription
    if not subscription:
        raise CliError(f"az functionapp show did not give the subscription of {a.app}: pass --subscription <id>")
    fx = ((app.get("siteConfig") or {}).get("linuxFxVersion") or "").split("|")[0].lower()
    return {"name": a.app, "resource_group": a.resource_group, "subscription_id": subscription, "runtime": fx}


# ── the functions: Node.js ──────────────────────────────────────────────────────────────────

_NODE_REG = re.compile(r"(?<![\w$.])app\s*\.\s*(http|get|post|put|patch|deleteRequest)\s*(\()\s*"
                       r"(['\"`])([^'\"`]+)\3\s*,\s*")


@dataclass
class _Registration:
    name: str
    start: int                    # the handler expression's span in the file
    end: int
    shorthand: bool               # `{ handler }`
    line: int

    def source(self, text: str) -> str:
        return text[self.start:self.end]


def node_registrations(text: str) -> list[_Registration]:
    """Each ``app.http('name', { handler })`` (or ``app.post('name', handler)``) in ``text``."""
    regs = []
    for m in _NODE_REG.finditer(text):
        pos, close = m.end(), ts.match_bracket(text, m.start(2))
        line = text.count("\n", 0, m.start()) + 1
        if text[pos:pos + 1] == "{":
            end = ts.match_bracket(text, pos)
            for key, value, offset in ts._top_level_entries(text[pos + 1:end]):
                if key != "handler":
                    continue
                at = pos + 1 + offset
                k = re.match(r"(?:['\"]handler['\"]|handler)\s*:\s*", text[at:])
                if k is None:
                    regs.append(_Registration(m.group(4), at, at + len("handler"), True, line))
                else:
                    regs.append(_Registration(m.group(4), at + k.end(), at + k.end() + len(value), False, line))
        else:
            expr = text[pos:close].rstrip().rstrip(",").rstrip()
            regs.append(_Registration(m.group(4), pos, pos + len(expr), False, line))
    return regs


def _function_range(text: str, name: str) -> tuple[int, int] | None:
    """Where ``function name(…) {…}`` or ``const name = (…) => {…}`` is in ``text``."""
    n = re.escape(name)
    m = re.search(rf"(?:async\s+)?function\s*\*?\s*{n}\s*(\()", text)
    if m:
        brace = text.find("{", ts.match_bracket(text, m.start(1)))
        return (m.start(), ts.match_bracket(text, brace)) if brace >= 0 else None
    m = re.search(rf"(?:const|let|var)\s+{n}\s*(?::[^=]*)?=(?!=)\s*(?:async\s+)?", text)
    if not m:
        return None
    i = m.end()
    if text.startswith("function", i):
        i = text.find("(", i)
    if text[i:i + 1] == "(":
        i = ts.match_bracket(text, i) + 1
    arrow = text.find("=>", i)
    brace = text.find("{", i)
    if brace < 0:
        return None
    if 0 <= arrow < brace and text[arrow + 2:brace].strip():         # an expression body: to the line's end
        nl = text.find("\n", arrow)
        return m.start(), (len(text) if nl < 0 else nl)
    return m.start(), ts.match_bracket(text, brace)


_IMPORT_NAMES = re.compile(r"""import\s+(?:type\s+)?([\w$*{}\s,]+?)\s+from\s*['"]([^'"]+)['"]"""
                           r"""|(?:const|let|var)\s+([\w${}\s,:]+?)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""")


def _imported_names(text: str) -> list[tuple[list[str], str]]:
    out = []
    for m in _IMPORT_NAMES.finditer(text):
        clause, spec = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
        names = []
        for part in re.split(r"[,{}]", clause):
            part = part.strip()
            if not part:
                continue
            alias = re.split(r"\s+as\s+|\s*:\s*", part)[-1].strip()
            if alias and alias != "*":
                names.append(alias.lstrip("*").strip())
        out.append((names, spec))
    return out


def node_functions(repo: Path) -> list[Function]:
    """The HTTP functions the app registers, each with the AI SDK calls it reaches."""
    repo = repo.resolve()
    files = ts.source_files(repo)
    by_file = ed.calls_by_file(ts.scan_model_calls(files))
    aliases = ed.alias_roots(repo)
    out: list[Function] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        regs = node_registrations(text)
        if not regs:
            continue
        reached = ed.reachable_calls(f, by_file, aliases)
        for reg in regs:
            if len(regs) == 1:
                calls = reached
            else:
                # Several functions in one file: the calls in this handler's own code, or behind the names
                # it uses that come from a module which calls the model.
                span = (reg.start, reg.end)
                if re.fullmatch(r"[A-Za-z_$][\w$]*", reg.source(text)):
                    span = _function_range(text, reg.source(text)) or span
                body = text[span[0]:span[1]]
                calls = [c for c in by_file.get(f.resolve(), []) if span[0] <= c.open_brace <= span[1]]
                for names, spec in _imported_names(text):
                    mod = ed._resolve(spec, f.resolve(), aliases)
                    if mod is not None and any(re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", body) for n in names):
                        calls += [c for c in ed.reachable_calls(mod, by_file, aliases) if c not in calls]
            out.append(Function(reg.name, f, reg.line, calls))
    return sorted(out, key=lambda fn: fn.name)


# ── the functions: Python ───────────────────────────────────────────────────────────────────


def vendored_dirs(repo: Path) -> set[Path]:
    """Folders that hold installed packages rather than the app's code: any folder with a ``*.dist-info`` in it,
    whatever its name (Lambda's ``package/`` and ``python/``, a ``pip install --target`` folder). Found in the
    live AWS test: connect edited the vendored openai library under ``package/``."""
    return {d.parent for d in repo.rglob("*.dist-info") if d.is_dir()}


def in_vendored(path: Path, vendored: set[Path]) -> bool:
    return any(v == path or v in path.parents for v in vendored)


def _py_files(repo: Path) -> list[Path]:
    out = []
    vendored = vendored_dirs(repo)
    for p in sorted(repo.rglob("*.py")):
        parts = p.relative_to(repo).parts[:-1]
        if not any(x in _PY_SKIP or x.startswith(".") for x in parts) and not in_vendored(p, vendored):
            out.append(p)
    return out


def _parse(path: Path) -> tuple[str, ast.Module | None]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return text, ast.parse(text)
    except SyntaxError:
        return text, None


def _model_calls(node: ast.AST) -> list[int]:
    return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Call) and instrument._call_kind(n.func)]


def _route_decorators(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, str | None]:
    """Whether ``fn`` is an HTTP function (``@<app>.route(…)``), and the name ``@<app>.function_name`` gives it."""
    http, name = False, None
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
            if d.func.attr == "route":
                http = True
            elif d.func.attr == "function_name":
                arg = next((k.value for k in d.keywords if k.arg == "name"), d.args[0] if d.args else None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    name = arg.value
    return http, name


def _module_file(repo: Path, here: Path, module: str | None, level: int) -> Path | None:
    base = here.parent if level else repo
    for _ in range(max(level - 1, 0)):
        base = base.parent
    parts = (module or "").split(".") if module else []
    cand = base.joinpath(*parts) if parts else base
    for p in (cand.with_suffix(".py"), cand / "__init__.py"):
        if p.is_file():
            return p.resolve()
    return None


def _reach(fn, f: Path, top: dict, imported: dict[str, Path], ai_modules: set[Path],
           seen: set[str]) -> list[tuple[Path, int]]:
    """The model calls ``fn`` makes: its own, its module's functions' that it calls, and (line 0) a local
    module's that it imports a name from."""
    found = [(f, line) for line in _model_calls(fn)]
    for n in ast.walk(fn):
        if not isinstance(n, ast.Name):
            continue
        if n.id in top and n.id not in seen and top[n.id] is not fn:
            seen.add(n.id)
            found += _reach(top[n.id], f, top, imported, ai_modules, seen)
        elif n.id in imported and imported[n.id] in ai_modules:
            found.append((imported[n.id], 0))
    return found


def python_functions(repo: Path) -> list[Function]:
    """The HTTP functions the app registers (``@app.route``), each with the model calls it reaches: in its
    own body, in the module's functions it calls, or in a local module it imports names from."""
    repo = repo.resolve()
    files = _py_files(repo)
    parsed = {f.resolve(): _parse(f) for f in files}
    from redberry_cli._scan import scan

    client_files = {Path(m.file).resolve() for m in scan(repo).model_candidates if m.file.endswith(".py")}
    ai_modules = {f for f, (_, tree) in parsed.items() if tree is not None and _model_calls(tree)} | client_files
    out: list[Function] = []
    for f, (text, tree) in parsed.items():
        if tree is None or "azure.functions" not in text and "azure import functions" not in text:
            continue
        top = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        imported: dict[str, Path] = {}
        for n in tree.body:
            if isinstance(n, ast.ImportFrom):
                mod = _module_file(repo, f, n.module, n.level)
                for alias in n.names:
                    sub = _module_file(repo, f, ".".join(x for x in (n.module, alias.name) if x), n.level)
                    target = sub or mod
                    if target is not None:
                        imported[alias.asname or alias.name] = target
            elif isinstance(n, ast.Import):
                for alias in n.names:
                    mod = _module_file(repo, f, alias.name, 0)
                    if mod is not None:
                        imported[alias.asname or alias.name.split(".")[0]] = mod

        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            http, name = _route_decorators(fn)
            if http:
                calls = list(dict.fromkeys(_reach(fn, f, top, imported, ai_modules, {fn.name})))
                out.append(Function(name or fn.name, f, fn.lineno, calls))
    return sorted(out, key=lambda fn: fn.name)


# ── naming ──────────────────────────────────────────────────────────────────────────────────


def name_agents(functions: list[Function], names: str | None) -> dict[str, str]:
    """agent name -> function name. By default each function that makes model calls, named after it;
    ``--name a,b`` names those in function order; ``--name a=fn,b=fn2`` picks any HTTP function."""
    agentic = [fn for fn in functions if fn.calls]
    known = {fn.name for fn in functions}
    if not names:
        if not agentic:
            listing = ", ".join(sorted(known)) or "none found"
            raise CliError("no HTTP function in the app reaches a model call the static reader can see "
                           f"(HTTP functions: {listing}): name the agents with --name agent=function,...")
        return {fn.name: fn.name for fn in agentic}
    items = [n.strip() for n in names.split(",") if n.strip()]
    if all("=" in i for i in items):
        out = {}
        for i in items:
            name, fn = (x.strip() for x in i.split("=", 1))
            if fn not in known:
                raise CliError(f"--name {i}: no HTTP function {fn} (found: {', '.join(sorted(known)) or 'none'})")
            out[name] = fn
        return out
    if any("=" in i for i in items):
        raise CliError("--name: either all `name` (in function order) or all `name=function`")
    if not agentic and len(items) == 1 and len(functions) == 1:
        return {items[0]: functions[0].name}            # the app's one HTTP function
    if len(items) != len(agentic):
        raise CliError(f"--name names {len(items)} agent(s) but {len(agentic)} function(s) make model calls "
                       f"({', '.join(fn.name for fn in agentic) or 'none'}): use --name name=function,...")
    return {n: fn.name for n, fn in zip(items, agentic)}




def the_agent(functions: list[Function], names: str | None, default: str) -> tuple[str, list[str]]:
    """The app's one agent name and the functions it covers. ``init()`` names every span the process sends,
    so an app is one agent: ``--name x`` (or no --name: ``default``) covers each function that makes model
    calls; ``--name x=fn,x=fn2`` picks the functions, all under the one name."""
    if names and "," not in names and "=" not in names:
        agentic = [fn.name for fn in functions if fn.calls] or ([functions[0].name] if len(functions) == 1 else [])
        if not agentic:
            raise CliError("no HTTP function in the app reaches a model call the static reader can see "
                           f"(HTTP functions: {', '.join(fn.name for fn in functions)}): pick them with "
                           f"--name {names.strip()}=function,...")
        return _agent_name(names), agentic
    agents = name_agents(functions, names)
    distinct = sorted(set(agents))
    if names and len(distinct) > 1:
        raise CliError(f"--name gives {len(distinct)} agent names ({', '.join(distinct)}), but redberry's init() names "
                       "every span a function app sends: give the app one name (--name x, or --name x=fn,x=fn2)")
    name = distinct[0] if names else default
    return _agent_name(name), sorted(agents.values())


def _agent_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise CliError("--name: 1 to 64 printable characters")
    return name


# ── the edits: Node.js ──────────────────────────────────────────────────────────────────────

#: What the Node.js wiring imports beside the package and its peers. ``ai`` only when the app has none.
NODE_DEPENDENCIES = {**ed.NODE_DEPENDENCIES, "@ai-sdk/otel": "^1.0.148"}
AI_SDK = {"ai": f"^{ed.AI_SDK_MAJOR}.0.0"}
AZURE_MONITOR = "@azure/monitor-opentelemetry"

SETUP_MODULE = """\
// Written by `redberry connect --platform azure-functions`. Every agent's function file imports it first.
// The app's spans go to the Redberry installation, which @redberrylabs/otel finds with the app's managed
// identity: there is nothing to configure.
{imports}
{init}// AI SDK 7 emits no spans until an integration is registered.
registerTelemetry(new OpenTelemetry({{ runtimeContext: true }}));
"""
_INIT = "init({{ agent: {agent} }});\n"
#: The earlier connect's wiring: the hasher's withRedberry around each handler, and its import.
_WITH_REDBERRY = re.compile(r"^withRedberry\(\s*(.*?)\s*,\s*\{[^{}]*\}\s*\)$", re.S)
_OLD_NODE_IMPORT = re.compile(r"(?m)^(?:import \{ withRedberry \} from '@redberrylabs/otel/node';|"
                              r"const \{ withRedberry \} = require\('@redberrylabs/otel/node'\);)[ \t]*\n")


def _is_cjs(text: str, path: Path, pkg: dict) -> bool:
    if path.suffix in (".cjs", ".cts"):
        return True
    if path.suffix in (".mjs", ".mts", ".ts", ".tsx"):
        return False
    return pkg.get("type") != "module" and "require(" in text and not re.search(r"(?m)^import\b", text)


def _setup_module(repo: Path, chosen: list[Function]) -> Path:
    """``src/redberry.<ext>`` (or ``redberry.<ext>`` when there is no src/), in the function files' language."""
    suffix = chosen[0].file.suffix if chosen else ".js"
    suffix = {".tsx": ".ts", ".jsx": ".js"}.get(suffix, suffix)
    root = repo / "src" if (repo / "src").is_dir() else repo
    return root / f"redberry{suffix}"


def _import_spec(module: Path, here: Path) -> str:
    """The relative import of the setup module from ``here``: with a .js extension for TypeScript, which
    resolves it to the .ts source and keeps it valid in ESM output."""
    rel = Path(os.path.relpath(module.with_suffix(module.suffix.replace("ts", "js")), here.parent)).as_posix()
    return rel if rel.startswith(".") else f"./{rel}"


def _top_of_file(text: str) -> int:
    """After a shebang and a leading 'use strict', where the setup import goes (before every other import)."""
    m = re.match(r"(?:#![^\n]*\n)?(?:\s*(?:'use strict'|\"use strict\");?[^\n]*\n)?", text)
    return m.end() if m else 0


def setup_module_text(agent: str, cjs: bool, monitor: bool) -> str:
    if cjs:
        imports = ("const { registerTelemetry } = require('ai');\n"
                   "const { OpenTelemetry } = require('@ai-sdk/otel');\n"
                   + ("" if monitor else "const { init } = require('@redberrylabs/otel/init');\n"))
    else:
        imports = ("import { registerTelemetry } from 'ai';\n"
                   "import { OpenTelemetry } from '@ai-sdk/otel';\n"
                   + ("" if monitor else "import { init } from '@redberrylabs/otel/init';\n"))
    return SETUP_MODULE.format(imports=imports, init="" if monitor else _INIT.format(agent=json.dumps(agent)))


def _node_edits(repo: Path, functions: list[Function], chosen_names: list[str], agent: str,
                notes: list[str]) -> list[ed.Edit]:
    edits: list[ed.Edit] = []
    pkg_path = repo / "package.json"
    pkg_text = pkg_path.read_text(encoding="utf-8")
    pkg = json.loads(pkg_text)
    present = _deps(pkg)
    monitor = AZURE_MONITOR in present
    wanted = {**NODE_DEPENDENCIES, **({} if "ai" in present else AI_SDK)}
    missing = {k: v for k, v in wanted.items() if k not in present} | ed.outdated(present, wanted)
    if "ai" not in present:
        notes.append(f"Warning: the app does not depend on the Vercel AI SDK (`ai`): its spans are what Redberry "
                     f"records; added `ai` {AI_SDK['ai']}, but the model calls must go through it.")
    elif (ed.major(present["ai"]) or ed.AI_SDK_MAJOR) < ed.AI_SDK_MAJOR:
        notes.append(f"Warning: the app uses AI SDK {ed.major(present['ai'])} (`ai: {present['ai']}`); the set-up "
                     f"registers AI SDK {ed.AI_SDK_MAJOR}'s telemetry. Upgrade `ai` before deploying.")
    if missing:
        edits.append(ed.package_json_edit(pkg_path, pkg_text, pkg, missing))

    by_fn = {fn.name: fn for fn in functions}
    chosen = sorted({by_fn[f].file: by_fn[f] for f in chosen_names}.values(), key=lambda fn: fn.file)
    module = _setup_module(repo, chosen)
    cjs_module = _is_cjs(chosen[0].file.read_text(encoding="utf-8"), chosen[0].file, pkg) if chosen else False
    wanted_module = setup_module_text(agent, cjs_module, monitor)
    if module.exists():
        current = module.read_text(encoding="utf-8")
        if "registerRedberry" in current:                    # the earlier connect's module: replace it
            edits.append(ed.Edit(module, current, wanted_module, "init() in place of the earlier registerRedberry()"))
        elif "@redberrylabs/otel/init" not in current and not monitor:
            notes.append(f"{module.relative_to(repo)} exists and is not Redberry's set-up module: move it, or make it "
                         "call init() from @redberrylabs/otel/init as the package's README shows.")
    else:
        edits.append(ed.Edit(module, None, wanted_module,
                             "init() and the AI SDK's OpenTelemetry integration once, at module load"))
    if monitor:
        notes.append(f"The app uses the Azure Monitor OpenTelemetry distro ({AZURE_MONITOR}), which registers the "
                     "tracer provider, so the set-up module does not call init(): give the distro Redberry's "
                     "exporter instead (the package's README, redberryExporter()), with the resource attribute "
                     f"redberry.agent.name = {json.dumps(agent)}.")
    if pkg.get("type") != "module" and module.suffix not in (".mjs", ".mts"):
        notes.append("Warning: @ai-sdk/otel is an ES module: make the app ESM (\"type\": \"module\" in package.json) or "
                     "run it on Node.js 22.12 or later, which can require it.")

    temps = ed.temperature_edits(repo, [c for f in chosen_names for c in by_fn[f].calls], notes)
    texts = {f: after for f, (_, after, _) in temps.items()}
    whys = {f: [why] for f, (_, _, why) in temps.items()}
    for f in [fn.file for fn in chosen]:
        text = texts.get(f) or f.read_text(encoding="utf-8")
        unwrapped = []
        for reg in sorted(node_registrations(text), key=lambda r: r.start, reverse=True):
            m = _WITH_REDBERRY.match(reg.source(text).strip())
            if m is None:
                continue
            inner = m.group(1)
            new = inner if not reg.shorthand else f"handler: {inner}"
            text = text[:reg.start] + new + text[reg.end:]
            unwrapped.append(reg.name)
        text = _OLD_NODE_IMPORT.sub("", text)
        if unwrapped:
            whys.setdefault(f, []).insert(0, "remove the earlier withRedberry from " + ", ".join(reversed(unwrapped)))
        spec = _import_spec(module, f)
        if not re.search(rf"""['"]{re.escape(spec)}['"]""", text):
            line = f"require('{spec}');" if _is_cjs(text, f, pkg) else f"import '{spec}';"
            at = _top_of_file(text)
            text = text[:at] + line + "\n" + text[at:]
            whys.setdefault(f, []).insert(0, f"import {spec} first")
        before = f.read_text(encoding="utf-8")
        if text != before:
            edits.append(ed.Edit(f, before, text, "; ".join(whys.get(f, []))))
    agent_files = {fn.file for fn in chosen}
    for f, (before, after, why) in temps.items():           # model calls in the code the handlers import
        if f not in agent_files:
            edits.append(ed.Edit(f, before, after, why))
    return edits


# ── the edits: Python ───────────────────────────────────────────────────────────────────────


def _python_temperatures(repo: Path, files: list[Path]) -> tuple[dict[Path, str], list[str]]:
    """``temperature=1.0`` on every OpenAI / Anthropic model call (``instrument.set_temperature``, run on a
    copy so nothing is written before it is agreed): file -> new text, and notes for what it can't change."""
    notes: list[str] = []
    out: dict[Path, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for f in files:
            dst = root / f.relative_to(repo)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
        for c in instrument.set_temperature(root):
            if c.provider_fixed:
                if c.before != "unset":
                    notes.append(f"{c.path.relative_to(root)}:{c.line}: the Anthropic call sends a temperature, "
                                 "which the Anthropic API (2026-10-21) rejects; remove it (that version fixes "
                                 "temperature at 1.0).")
            elif c.before == "variable":
                notes.append(f"{c.path.relative_to(root)}:{c.line}: the model call sets temperature from a "
                             "variable; make sure it is 1.0 (pricing needs exactly 1.0).")
        for f in files:
            new = (root / f.relative_to(repo)).read_text(encoding="utf-8")
            if new != f.read_text(encoding="utf-8"):
                out[f] = new
    return out, notes


def _python_edits(repo: Path, functions: list[Function], chosen_names: list[str], agent: str,
                  notes: list[str]) -> list[ed.Edit]:
    edits: list[ed.Edit] = []
    req = ed.py_requirement_edit(repo / "requirements.txt")
    if req is not None:
        edits.append(req)
    files = _py_files(repo)
    texts, temp_notes = _python_temperatures(repo, files)
    notes += temp_notes
    whys: dict[Path, list[str]] = {f: ["temperature=1.0 on its model calls"] for f in texts}
    by_fn = {fn.name: fn for fn in functions}
    agent_files = sorted({by_fn[fn].file.resolve() for fn in chosen_names})
    entry = (repo / "function_app.py").resolve()
    has_init = any("redberry_sdk.init(" in f.read_text(encoding="utf-8", errors="replace") for f in files)
    init_in = None if has_init else (entry if entry.exists() else agent_files[0] if agent_files else None)
    old = {f.resolve() for f in files if "redberry_sdk.hasher" in f.read_text(encoding="utf-8", errors="replace")
           or "with_redberry" in f.read_text(encoding="utf-8", errors="replace")}
    for f in sorted(set(agent_files) | ({init_in} if init_in else set()) | old):
        text = texts.get(f) or f.read_text(encoding="utf-8")
        tree = ast.parse(text)
        # The Python function's own name: what @app.function_name calls it may differ.
        defs = set()
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                http, name = _route_decorators(n)
                if http and (name or n.name) in chosen_names:
                    defs.add(n.name)
        new, why = ed.py_wire(text, agent, defs, f == init_in, str(f.relative_to(repo)), notes)
        if why:
            whys.setdefault(f, [])[:0] = why
        texts[f] = new
    for f, new in sorted(texts.items()):
        before = f.read_text(encoding="utf-8")
        if new != before:
            edits.append(ed.Edit(f, before, new, "; ".join(whys.get(f, []))))
    return edits


def plan_edits(repo: Path, runtime: str, functions: list[Function], chosen: list[str], agent: str
               ) -> tuple[list[ed.Edit], list[str]]:
    """The app's edits: ``init`` under the name ``agent``, for the functions named in ``chosen``."""
    notes: list[str] = []
    repo = repo.resolve()
    if runtime == "node":
        edits = _node_edits(repo, functions, chosen, agent, notes)
    else:
        edits = _python_edits(repo, functions, chosen, agent, notes)
    return edits, notes


# ── the command ─────────────────────────────────────────────────────────────────────────────


def _record(repo: Path) -> dict:
    try:
        return json.loads((repo / PROJECT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _same_app(record: dict, app: dict) -> bool:
    saved = record.get("azure_function_app") or {}
    return saved.get("name") == app["name"] and saved.get("resource_group") == app["resource_group"]


def cmd_connect(a, out=print, run=None, ask=None) -> int:
    run = run or subprocess.run
    ask = ask or ed.asker(a)
    say = (lambda _m: None) if a.json else out
    if not a.app or not a.resource_group:
        raise CliError("--platform azure-functions needs --app <function app name> and --resource-group <group>")
    repo = Path(a.repo).resolve()
    runtime, notes = detect_runtime(repo)
    record = _record(repo)

    app = function_app(a, run)
    if app["runtime"] and app["runtime"] != runtime:
        notes.append(f"Warning: Azure runs {a.app} on {app.pop('runtime')}, but this folder is a {runtime} app.")
    app.pop("runtime", None)
    functions = node_functions(repo) if runtime == "node" else python_functions(repo)
    if not functions:
        raise CliError("no HTTP-triggered function found ("
                       + ("app.http('name', { handler })" if runtime == "node" else "@app.route(...)")
                       + "): nothing to connect")
    default = (record.get("agent_name") if _same_app(record, app) else None) or app["name"]
    agent, chosen = the_agent(functions, a.name, default)
    if runtime == "node":
        tools = [{"name": t.name, "description": t.description, "parameters_schema": t.parameters_schema,
                  "file": t.file, "line": t.line} for t in ts.scan_tools(ts.source_files(repo), repo)]
    else:
        from redberry_cli._scan import scan

        tools = [{"name": t.name, "description": t.description, "parameters_schema": t.parameters_schema,
                  "file": str(Path(t.file).resolve().relative_to(repo)) if Path(t.file).is_absolute() else t.file,
                  "line": t.line} for t in scan(repo).static_tools]

    by_fn = {fn.name: fn for fn in functions}
    say(f"Function app: {app['name']} ({app['resource_group']}, subscription {app['subscription_id']}), {runtime}")
    say(f"  agent {agent}")
    for fn in chosen:
        f = by_fn[fn]
        seen = f"{len(f.calls)} model call(s) found" if f.calls else "no model call found by the static reader"
        say(f"    function {fn} ({f.file.relative_to(repo)}:{f.line}; {seen})")
    say(f"  tools: {', '.join(t['name'] for t in tools) or 'none found by the static reader'}")

    edits, edit_notes = plan_edits(repo, runtime, functions, chosen, agent)
    notes += edit_notes
    if edits:
        say("Changes to the app:")
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
        "platform": PLATFORM, "azure_function_app": app, "runtime": runtime, "agent_name": agent,
        "functions": [{"function": fn, "file": str(by_fn[fn].file.relative_to(repo))} for fn in chosen],
        "tools": tools,
    }
    (repo / PROJECT_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / PROJECT_FILE).write_text(json.dumps(description, indent=2) + "\n", encoding="utf-8")

    install = [] if runtime != "node" else ["pnpm install" if (repo / "pnpm-lock.yaml").exists() else
                                            "yarn" if (repo / "yarn.lock").exists() else "npm install"]
    deploy = f"func azure functionapp publish {app['name']}"
    notes.append(f"{app['name']} needs a managed identity that was chosen as an agent when Redberry was installed: "
                 "init() finds the installation with it. If it was not, run the install again and select it.")
    result = {
        "platform": PLATFORM, "azure_function_app": app, "runtime": runtime, "agent_name": agent,
        "functions": chosen, "edited": [str(e.path.relative_to(repo)) for e in edits], "notes": notes,
        "next": [*install, deploy],
    }
    if a.json:
        out(json.dumps(result, indent=2))
        return 0
    for n in notes:
        out(n)
    if edits:
        out(f"Edited {', '.join(result['edited'])}; wrote {PROJECT_FILE}")
    else:
        out(f"{app['name']} is already wired for Redberry; wrote {PROJECT_FILE}")
    out(f"Next: {' then '.join([*install, deploy])}. connect never deploys.")
    return 0


__all__ = ["PY_REQUIREMENT", "Function", "cmd_connect", "detect_runtime", "function_app", "name_agents",
           "node_functions", "node_registrations", "plan_edits", "python_functions", "the_agent"]
