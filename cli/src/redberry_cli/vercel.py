"""``redberry connect --platform vercel``: a Next.js agent on Vercel, using the Vercel AI SDK.

The app's deployment sends its spans to the customer's Redberry install: ``@vercel/otel``'s ``registerOTel``
in ``instrumentation.ts``, with ``@redberrylabs/otel``'s ``redberryExporter(exporterOptionsFrom({}))`` as
its trace exporter and the agent's name as the resource attribute ``redberry.agent.name``. The exporter
exchanges the deployment's Vercel OIDC token for a short-lived credential on the install's cloud: there
is no key in the project. connect:

1. checks the app: a ``package.json`` with ``next`` and ``ai`` (AI SDK 7 is what the set-up registers
   telemetry for: below 7 is a warning), and at least one App Router route that reaches an AI SDK call
   (``redberry_cli._ts_tools``, following local imports);
2. names the agent: ``--name``, else the name an earlier connect recorded, else the Vercel project's name
   (``.vercel/project.json``), else the package's. The app is one agent: every span it sends carries it;
3. writes ``instrumentation.ts`` (``src/instrumentation.ts`` in a ``src/app`` layout), the dependencies in
   ``package.json``, ``temperature: 1`` on each model call the routes reach (pricing needs exactly 1) and
   ``.redberry/vercel.json`` (what was found; no secret), after showing the diff and asking (``--yes``
   applies without asking). An ``instrumentation.ts`` an earlier connect wrote for the in-process hasher
   (``RedberryProcessor``) is replaced;
4. the ``REDBERRY_*`` variables the exporter reads come from the install: run with ``--vercel-team``, it
   prints them (``REDBERRY_CLOUD``, ``REDBERRY_INGEST_URL`` and the cloud's federation names). connect takes
   them from ``--env-file`` (that output, saved to a file) or from the same variables in its own
   environment, checks the set is complete for the cloud, and sets them on the project's production
   environment with ``vercel env add`` (none is secret). With none given, or ``--no-env``, or no linked
   project or Vercel CLI, it prints what to set instead.

It never deploys: the last step is yours, commit and deploy.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from redberry_cli import _edits as ed
from redberry_cli import _ts_tools as ts
from redberry_cli.main import CliError

PLATFORM = "vercel"
PROJECT_FILE = Path(".redberry") / "vercel.json"
#: npm specs for what ``instrumentation.ts`` imports. 0.6.0: the ingest exporter under PolyForm Shield.
DEPENDENCIES = {
    "@redberrylabs/otel": "^0.6.0",
    "@vercel/otel": "^2.1.3",
    "@ai-sdk/otel": "^1.0.148",
    "@opentelemetry/api": "^1.9.0",
    "@opentelemetry/sdk-trace-base": "^2.0.0",
    "@opentelemetry/resources": "^2.0.0",
    "@opentelemetry/context-async-hooks": "^2.0.0",
}

#: The variables the exporter reads on Vercel (``exporterOptionsFrom``), by the install's cloud: what each
#: install prints for ``--vercel-team``. ``optional`` ones have defaults in the package.
REQUIRED = {
    "gcp": ("REDBERRY_INGEST_URL", "REDBERRY_GCP_PROJECT_NUMBER", "REDBERRY_GCP_POOL_ID", "REDBERRY_GCP_PROVIDER_ID",
            "REDBERRY_GCP_SERVICE_ACCOUNT"),
    "aws": ("REDBERRY_INGEST_URL", "REDBERRY_AWS_REGION", "REDBERRY_AWS_ROLE_ARN"),
    "azure": ("REDBERRY_INGEST_URL", "REDBERRY_AZURE_TENANT_ID", "REDBERRY_AZURE_CLIENT_ID"),
}
OPTIONAL = {"gcp": (), "aws": (), "azure": ("REDBERRY_AZURE_APP_ID_URI", "REDBERRY_AZURE_SCOPE")}
#: What the in-process hasher's connect set on the project; the exporter reads none of them.
OLD_VARIABLES = ("REDBERRY_PROJECT_TOKEN", "REDBERRY_URL", "REDBERRY_SIGNING_KEY", "REDBERRY_KEY_ID",
                 "REDBERRY_STORE", "REDBERRY_AGENTS")

INSTRUMENTATION = """\
// Written by `redberry connect --platform vercel`.
// The app's spans go to the Redberry install: @redberrylabs/otel exchanges the deployment's Vercel OIDC token
// for a short-lived credential. Its settings are the REDBERRY_* environment variables of the Vercel project.
import {{ registerOTel }} from '@vercel/otel';

export async function register() {{
  // AI SDK 7 and Redberry run on the Node.js runtime only.
  if (process.env.NEXT_RUNTIME !== 'nodejs') return;
  const {{ redberryExporter, exporterOptionsFrom, validateAgentName }} = await import('@redberrylabs/otel');
  const {{ registerTelemetry }} = await import('ai');
  const {{ OpenTelemetry }} = await import('@ai-sdk/otel');
  registerOTel({{
    serviceName: {name},
    // The resource attribute the install files the spans under.
    attributes: {{ 'redberry.agent.name': validateAgentName({name}) }},
    // Endpoint and credentials from the REDBERRY_* variables.
    traceExporter: redberryExporter(exporterOptionsFrom({{}})),
  }});
  // AI SDK 7 emits no spans until an integration is registered.
  registerTelemetry(new OpenTelemetry({{ runtimeContext: true }}));
}}
"""
#: The first line of every instrumentation.ts connect writes; with ``RedberryProcessor`` in it, the
#: earlier, in-process hasher's version, which connect replaces.
_OURS = "// Written by `redberry connect --platform vercel`."


@dataclass
class Route:
    path: str                          # "/api/refund"
    file: Path
    calls: list[ts.ModelCall] = field(default_factory=list)


# ── the app ─────────────────────────────────────────────────────────────────────────────────


def check_app(repo: Path) -> tuple[dict, str, list[str]]:
    """The parsed package.json, its text and warnings. Refuses an app that is not Next.js with the AI SDK."""
    pkg_path = repo / "package.json"
    if not pkg_path.exists():
        raise CliError(f"no package.json in {repo}: run connect in the Next.js app (or --repo)")
    text = pkg_path.read_text(encoding="utf-8")
    try:
        pkg = json.loads(text)
    except ValueError as e:
        raise CliError(f"{pkg_path} is not valid JSON ({e})") from None
    deps = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
    if "next" not in deps:
        raise CliError(f"{repo} is not a Next.js app (no `next` in package.json): --platform vercel is "
                       "for Next.js apps using the Vercel AI SDK")
    ai = deps.get("ai")
    if ai is None:
        raise CliError("the app does not depend on the Vercel AI SDK (`ai` in package.json)")
    notes = []
    major = ed.major(ai)
    if major is None:
        notes.append(f"Warning: cannot read the AI SDK version from `ai: {ai}`; the set-up registers AI SDK "
                     f"{ed.AI_SDK_MAJOR}'s telemetry.")
    elif major < ed.AI_SDK_MAJOR:
        notes.append(f"Warning: the app uses AI SDK {major} (`ai: {ai}`); the set-up registers AI SDK "
                     f"{ed.AI_SDK_MAJOR}'s telemetry. Upgrade `ai` to ^{ed.AI_SDK_MAJOR} before deploying.")
    return pkg, text, notes


def linked_project(repo: Path) -> dict | None:
    """``{id, name, org_id}`` from ``.vercel/project.json`` (``vercel link``), or None."""
    try:
        p = json.loads((repo / ".vercel" / "project.json").read_text(encoding="utf-8"))
        return {"id": p["projectId"], "name": p.get("projectName"), "org_id": p.get("orgId")}
    except (OSError, ValueError, KeyError):
        return None


def _route_path(app_dir: Path, route_file: Path) -> str:
    parts = [p for p in route_file.parent.relative_to(app_dir).parts
             if not (p.startswith("(") and p.endswith(")")) and not p.startswith("@")]
    return "/" + "/".join(parts)


def agent_routes(repo: Path, calls: list[ts.ModelCall]) -> list[Route]:
    """Each App Router route handler whose code (or the local code it imports) calls the AI SDK."""
    by_file = ed.calls_by_file(calls)
    aliases = ed.alias_roots(repo)
    routes: list[Route] = []
    for app_dir in (repo / "app", repo / "src" / "app"):
        if not app_dir.is_dir():
            continue
        for f in ts.source_files(app_dir):
            if f.stem != "route":
                continue
            found = ed.reachable_calls(f, by_file, aliases)
            if found:
                routes.append(Route(path=_route_path(app_dir, f), file=f, calls=found))
    return sorted(routes, key=lambda r: r.path)


# ── the environment ─────────────────────────────────────────────────────────────────────────


_LINE = re.compile(r"(?m)(?:^|\s)(REDBERRY_[A-Z0-9_]+)=(\S+)")


def read_env_file(path: Path) -> dict[str, str]:
    """The ``REDBERRY_*=value`` lines in ``path``: the install's output pasted as it is (indented, or after a
    log prefix), or a .env file. Other lines are ignored."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise CliError(f"--env-file {path}: {e.strerror or e}") from None
    return {k: v.strip().strip("'\"") for k, v in _LINE.findall(text)}


def env_values(given: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The variables to set, from what was given: the cloud's required ones and any optional ones. An empty
    result means none was given. Raises when the set is incomplete or names an unknown cloud."""
    if not given:
        return {}, []
    cloud = given.get("REDBERRY_CLOUD", "")
    if cloud not in REQUIRED:
        raise CliError(f"REDBERRY_CLOUD is {cloud or 'not set'}: gcp, aws or azure, as the install printed it")
    missing = [k for k in REQUIRED[cloud] if not given.get(k)]
    if missing:
        raise CliError(f"for REDBERRY_CLOUD={cloud} the install also prints {', '.join(missing)}: add "
                       f"{'it' if len(missing) == 1 else 'them'} to --env-file (or the environment)")
    if not given["REDBERRY_INGEST_URL"].startswith("https://"):
        raise CliError(f"REDBERRY_INGEST_URL must be an https:// URL, not {given['REDBERRY_INGEST_URL']!r}")
    keys = ("REDBERRY_CLOUD", *REQUIRED[cloud], *OPTIONAL[cloud])
    ignored = sorted(k for k in given if k not in keys)
    return {k: given[k] for k in keys if given.get(k)}, ignored


def set_vercel_env(env: dict[str, str], repo: Path, run) -> list[str]:
    """``vercel env add NAME production --force`` for each, the value on stdin (``--force`` replaces the value
    an earlier run set). Returns the names set."""
    done = []
    for name, value in env.items():
        cmd = ["vercel", "env", "add", name, "production", "--force", "--yes"]
        try:
            r = run(cmd, cwd=repo, input=value, capture_output=True, text=True)
        except OSError:
            raise CliError("the Vercel CLI is not installed: npm i -g vercel; or rerun with --no-env") from None
        if r.returncode != 0:
            raise CliError(f"`vercel env add {name} production` failed: "
                           f"{(getattr(r, 'stderr', '') or '').strip() or r.returncode}. Set: {', '.join(done) or 'none'}")
        done.append(name)
    return done


# ── the edits ───────────────────────────────────────────────────────────────────────────────


def _instrumentation_path(repo: Path) -> Path:
    if (repo / "src" / "app").is_dir() and not (repo / "app").is_dir():
        return repo / "src" / "instrumentation.ts"
    return repo / "instrumentation.ts"


def plan_edits(repo: Path, pkg: dict, pkg_text: str, agent: str, routes: list[Route]
               ) -> tuple[list[ed.Edit], list[str]]:
    edits: list[ed.Edit] = []
    notes: list[str] = []
    inst = _instrumentation_path(repo)
    wanted = INSTRUMENTATION.format(name=json.dumps(agent))
    existing = next((p for p in (inst, inst.with_suffix(".js")) if p.exists()), None)
    if existing is None:
        edits.append(ed.Edit(inst, None, wanted, "register @redberrylabs/otel's exporter with @vercel/otel and the "
                                                 "AI SDK's OpenTelemetry integration"))
    else:
        text = existing.read_text(encoding="utf-8")
        if text.startswith(_OURS) and "RedberryProcessor" in text:
            edits.append(ed.Edit(existing, text, wanted, "the ingest exporter in place of the earlier RedberryProcessor"))
        elif text.startswith(_OURS) and "redberryExporter" in text:
            if f"validateAgentName({json.dumps(agent)})" not in text:
                edits.append(ed.Edit(existing, text, wanted, f"name the agent {agent}"))
        elif "redberryExporter" not in text:
            notes.append(f"{existing.relative_to(repo)} exists and does not send to Redberry; merge this into its "
                         "register() (one registerOTel per app):\n" + wanted)

    present = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
    missing = {k: v for k, v in DEPENDENCIES.items() if k not in present} | ed.outdated(present, DEPENDENCIES)
    if missing:
        edits.append(ed.package_json_edit(repo / "package.json", pkg_text, pkg, missing))

    # temperature: 1 on every model call (pricing refuses anything else, or nothing recorded).
    for f, (before, text, why) in ed.temperature_edits(repo, [c for r in routes for c in r.calls], notes).items():
        edits.append(ed.Edit(f, before, text, why))
    return edits, notes


# ── the command ─────────────────────────────────────────────────────────────────────────────


def _record(repo: Path) -> dict:
    """What an earlier connect recorded (.redberry/vercel.json), or {}."""
    try:
        return json.loads((repo / PROJECT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _agent_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise CliError("--name: 1 to 64 printable characters")
    return name


def _given(a, environ: dict[str, str]) -> dict[str, str]:
    """REDBERRY_* from the environment, then --env-file over them."""
    given = {k: v for k, v in environ.items() if k.startswith("REDBERRY_") and v}
    given.pop("REDBERRY_API_URL", None)                 # the CLI's own setting, not the exporter's
    if getattr(a, "env_file", None):
        given.update(read_env_file(Path(a.env_file)))
    return given


def multi_route_note(agent: str, routes: list[Route]) -> str | None:
    """When more than one route calls a model: they are one agent (one name per app), so say so."""
    if len(routes) < 2:
        return None
    return (f"This app has model calls in {len(routes)} routes ({', '.join(r.path for r in routes)}); they are "
            f"recorded and priced as one agent, {agent}. To price agents separately, deploy them as separate "
            "Vercel projects.")


def cmd_connect(a, out=print, run=None, ask=None, environ=None) -> int:
    import os
    import shutil

    run = run or subprocess.run
    ask = ask or ed.asker(a)
    environ = os.environ if environ is None else environ
    say = (lambda _m: None) if a.json else out
    repo = Path(a.repo).resolve()
    record = _record(repo)
    pkg, pkg_text, notes = check_app(repo)
    env, ignored = env_values(_given(a, environ))
    if ignored:
        notes.append(f"Not set (the exporter does not read them for REDBERRY_CLOUD={env['REDBERRY_CLOUD']}): "
                     f"{', '.join(ignored)}")

    files = ts.source_files(repo)
    routes = agent_routes(repo, ts.scan_model_calls(files))
    if not routes:
        raise CliError("no route handler in app/ calls the AI SDK (streamText, generateText, ToolLoopAgent): "
                       "nothing to connect")
    project = linked_project(repo)
    agent = _agent_name(a.name or record.get("agent_name") or (project or {}).get("name") or pkg.get("name") or "")
    tools = ts.scan_tools(files, repo)

    say(f"Vercel project: {project['name'] or project['id']}" if project else
        "Vercel project: not linked here (.vercel/project.json; `vercel link` links it)")
    say(f"  agent {agent}: {', '.join(r.path for r in routes)}")
    say(f"  tools: {', '.join(t.name for t in tools) or 'none found by the static reader'}")
    routes_note = multi_route_note(agent, routes)
    if routes_note:
        say(routes_note)

    edits, edit_notes = plan_edits(repo, pkg, pkg_text, agent, routes)
    notes += edit_notes
    can_set = bool(env) and not a.no_env and project is not None and shutil.which("vercel") is not None
    if edits:
        say("Changes to the app:")
        for e in edits:
            say(f"  {e.path.relative_to(repo)}: {e.why}")
        if not a.json:
            out(ed.diff(edits, repo).rstrip())
    if edits or can_set:
        question = ("Apply these changes" if edits else "") + (" and " if edits and can_set else "") + \
            (f"set {len(env)} REDBERRY_* variables on the Vercel project's production environment" if can_set else "")
        if not a.yes and not ask(question[:1].upper() + question[1:] + "?"):
            raise CliError("nothing changed" + ("; --yes applies without asking"
                                                if a.json or not sys.stdin.isatty() else ""))
    for e in edits:
        e.path.parent.mkdir(parents=True, exist_ok=True)
        e.path.write_text(e.after, encoding="utf-8")
    description = {
        "platform": PLATFORM, **({"vercel_project": {"id": project["id"], "name": project["name"]}} if project else {}),
        "agent_name": agent, "routes": [r.path for r in routes],
        **({"cloud": env["REDBERRY_CLOUD"]} if env else {}),
        "tools": [{"name": t.name, "description": t.description, "parameters_schema": t.parameters_schema,
                   "file": t.file, "line": t.line} for t in tools],
    }
    (repo / PROJECT_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / PROJECT_FILE).write_text(json.dumps(description, indent=2) + "\n", encoding="utf-8")

    env_set = set_vercel_env(env, repo, run) if can_set else []
    if record.get("public_key") or record.get("store"):
        notes.append("This app was connected before with the in-process hasher: remove its variables from the "
                     f"Vercel project ({', '.join(OLD_VARIABLES)}); REDBERRY_SIGNING_KEY and REDBERRY_PROJECT_TOKEN "
                     "are secrets, and nothing reads them now.")
    notes.append("The exporter needs Vercel's OIDC federation on (Settings > Security > Secure backend access, team "
                 "issuer), and the install run with --vercel-team for this team (and the project, where it asks).")
    install = ("pnpm install" if (repo / "pnpm-lock.yaml").exists() else "yarn" if (repo / "yarn.lock").exists()
               else "bun install" if (repo / "bun.lockb").exists() or (repo / "bun.lock").exists()
               else "npm install")
    result = {
        "platform": PLATFORM, "vercel_project": description.get("vercel_project"), "agent_name": agent,
        "routes": description["routes"], "edited": [str(e.path.relative_to(repo)) for e in edits],
        "env": env, "env_set": env_set, "notes": notes, "routes_note": routes_note,
        "next": [install, "commit and deploy"],
    }
    if a.json:
        out(json.dumps(result, indent=2))
        return 0
    for n in notes:
        out(n)
    if edits:
        out(f"Edited {', '.join(result['edited'])}; wrote {PROJECT_FILE}")
    if env_set:
        out(f"Set {len(env_set)} variables on the Vercel project's production environment: {', '.join(env_set)}. "
            "For preview deployments, add them to Preview too.")
    elif env:
        why = ("--no-env" if a.no_env else "the app is not linked: run vercel link" if project is None
               else "the Vercel CLI is not installed")
        out(f"Not set on Vercel ({why}). Set these on the project (Settings > Environment Variables), none secret:")
        for k, v in env.items():
            out(f"  {k}={v}")
    else:
        out("Set the REDBERRY_* variables the install printed for --vercel-team on the Vercel project (Settings > "
            "Environment Variables), or run connect again with --env-file <that output>: REDBERRY_CLOUD, "
            "REDBERRY_INGEST_URL and the cloud's federation names.")
    out(f"Next: {install}, then commit and deploy. connect never deploys.")
    return 0


__all__ = ["DEPENDENCIES", "INSTRUMENTATION", "agent_routes", "check_app", "cmd_connect", "env_values",
           "linked_project", "multi_route_note", "plan_edits", "read_env_file", "set_vercel_env"]
