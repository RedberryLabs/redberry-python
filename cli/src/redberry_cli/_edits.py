"""What ``redberry connect --platform aws-lambda | azure-functions`` shares: the edits it proposes to an app's
code (shown as a diff, written only once agreed), the npm-range checks for ``package.json``, and the static
walk from a handler to the AI SDK calls it reaches, through the local code it imports.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from redberry_cli import _ts_tools as ts

#: The Node.js package connect wires, and the release whose ``init()`` (``@redberrylabs/otel/init``) it calls.
NODE_PACKAGE = "@redberrylabs/otel"
NODE_PACKAGE_RANGE = "^0.5.0"
#: What ``init()`` needs beside the package (its peer dependencies, from the package's README).
NODE_DEPENDENCIES = {
    NODE_PACKAGE: NODE_PACKAGE_RANGE,
    "@opentelemetry/api": "^1.9.0",
    "@opentelemetry/sdk-trace-base": "^2.0.0",
    "@opentelemetry/context-async-hooks": "^2.0.0",
    "@opentelemetry/resources": "^2.0.0",
}
#: The Python SDK, from PyPI. ``providers``: init() refuses to start when a provider library (openai,
#: anthropic) is installed without its instrumentation, since a run recorded without its model calls cannot
#: be priced.
PY_REQUIREMENT = "redberry-sdk[providers]"
AI_SDK_MAJOR = 7


@dataclass
class Edit:
    path: Path
    before: str | None                 # None: a new file
    after: str
    why: str


def diff(edits: list[Edit], repo: Path) -> str:
    chunks = []
    for e in edits:
        rel = str(e.path.relative_to(repo))
        chunks.append("".join(difflib.unified_diff(
            (e.before or "").splitlines(keepends=True), e.after.splitlines(keepends=True),
            fromfile="/dev/null" if e.before is None else f"a/{rel}", tofile=f"b/{rel}")))
    return "\n".join(chunks)


def asker(a) -> Callable[[str], bool]:
    """A yes/no question on the terminal; no (never a prompt) under --json or with no terminal."""
    def ask(question: str) -> bool:
        if a.json or not sys.stdin.isatty():
            return False
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    return ask


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` readable by its owner only (created so, never briefly wider)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)


# ── npm ranges ──────────────────────────────────────────────────────────────────────────────


def major(spec: str | None) -> int | None:
    m = re.search(r"(\d+)", spec or "")
    return int(m.group(1)) if m else None


def _version(spec: str | None) -> tuple[int, ...] | None:
    """The lowest version an npm range like ``^0.1.0`` or ``~1.2`` accepts, as a tuple; None if unreadable."""
    m = re.match(r"\s*[\^~>=]*\s*v?(\d+(?:\.\d+){0,2})", spec or "")
    return tuple(int(x) for x in m.group(1).split(".")) + (0,) * (2 - m.group(1).count(".")) if m else None


def outdated(deps: dict[str, str], wanted: dict[str, str], names: tuple[str, ...] = (NODE_PACKAGE,)) -> dict[str, str]:
    """Of ``names``, those already in ``deps`` at a range below ``wanted``'s: ``^0.3.0`` never installs
    0.5.0, so a connect that needs a newer package must raise the range, not only add missing ones."""
    out = {}
    for n in names:
        have, want = _version(deps.get(n)), _version(wanted.get(n))
        if n in deps and n in wanted and have is not None and want is not None and have < want:
            out[n] = wanted[n]
    return out


def package_json_edit(pkg_path: Path, pkg_text: str, pkg: dict, missing: dict[str, str]) -> Edit:
    """``package.json`` with ``missing`` added to its dependencies, in its own indentation."""
    deps = dict(pkg.get("dependencies") or {})
    deps.update(missing)
    pkg = {**pkg, "dependencies": dict(sorted(deps.items()))}
    indent = re.match(r"\{\s*\n([ \t]+)", pkg_text)
    after = json.dumps(pkg, indent=indent.group(1) if indent else 2, ensure_ascii=False) + "\n"
    return Edit(pkg_path, pkg_text, after, "add " + ", ".join(missing))


# ── from a handler to its model calls ───────────────────────────────────────────────────────


def alias_roots(repo: Path) -> list[tuple[str, Path]]:
    """tsconfig `paths` prefixes (``@/`` -> ``./``) as (prefix, directory)."""
    out = []
    for name in ("tsconfig.json", "jsconfig.json"):
        p = repo / name
        if not p.exists():
            continue
        try:
            text = re.sub(r"(?m)^\s*//.*$", "", p.read_text(encoding="utf-8"))
            paths = (json.loads(re.sub(r",(\s*[}\]])", r"\1", text)).get("compilerOptions") or {}).get("paths") or {}
        except ValueError:
            continue
        for alias, targets in paths.items():
            if alias.endswith("/*") and targets:
                out.append((alias[:-1], (repo / targets[0].rstrip("*")).resolve()))
    if not out:
        out.append(("@/", (repo / "src").resolve() if (repo / "src").is_dir() else repo.resolve()))
    return out


_IMPORT = re.compile(r"""(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)""")


def _resolve(spec: str, here: Path, aliases: list[tuple[str, Path]]) -> Path | None:
    if spec.startswith("."):
        base = (here.parent / spec).resolve()
    else:
        alias = next(((pre, root) for pre, root in aliases if spec.startswith(pre)), None)
        if alias is None:
            return None
        base = (alias[1] / spec[len(alias[0]):]).resolve()
    for cand in [base, *(base.with_name(base.name + s) for s in ts.SOURCE_SUFFIXES),
                 *(base / f"index{s}" for s in ts.SOURCE_SUFFIXES)]:
        if cand.is_file():
            return cand
    return None


def calls_by_file(calls: list[ts.ModelCall]) -> dict[Path, list[ts.ModelCall]]:
    by_file: dict[Path, list[ts.ModelCall]] = {}
    for c in calls:
        by_file.setdefault(c.file.resolve(), []).append(c)
    return by_file


def reachable_calls(start: Path, by_file: dict[Path, list[ts.ModelCall]],
                    aliases: list[tuple[str, Path]]) -> list[ts.ModelCall]:
    """The model calls in ``start`` and in the local code it imports, transitively."""
    seen, queue, found = set(), [start.resolve()], []
    while queue and len(seen) < 200:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        found += by_file.get(cur, [])
        try:
            text = cur.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _IMPORT.finditer(text):
            nxt = _resolve(m.group(1) or m.group(2), cur, aliases)
            if nxt is not None and nxt not in seen:
                queue.append(nxt)
    return found


def temperature_edits(repo: Path, calls: list[ts.ModelCall], notes: list[str]) -> dict[Path, tuple[str, str, str]]:
    """``temperature: 1`` on each of ``calls``: file -> (text before, text after, what changed), for the
    files that change. What it cannot change is said in ``notes``."""
    files: dict[Path, list[ts.ModelCall]] = {}
    for c in calls:
        if c not in files.setdefault(c.file, []):
            files[c.file].append(c)
    out: dict[Path, tuple[str, str, str]] = {}
    for f, file_calls in sorted(files.items()):
        before = text = f.read_text(encoding="utf-8")
        changed = []
        # From the end of the file, so earlier offsets stay valid.
        for c in sorted(file_calls, key=lambda c: c.open_brace, reverse=True):
            if c.temperature is None and c.has_spread:
                notes.append(f"{f.relative_to(repo)}:{c.line}: {c.kind}() takes its options from a spread "
                             "object; make sure it sets temperature: 1 (pricing needs exactly 1).")
                continue
            text, what = ts.set_temperature_one(text, c)
            if what == "variable":
                notes.append(f"{f.relative_to(repo)}:{c.line}: {c.kind}() sets temperature from a "
                             "variable; make sure it is 1 (pricing needs exactly 1).")
            elif what != "kept":
                changed.append(f"{c.kind}() line {c.line}: temperature {what}")
        if text != before:
            out[f] = (before, text, "; ".join(reversed(changed)))
    return out


# ── Python: redberry_sdk.init() and flush_after ─────────────────────────────────────────────

#: A requirement line for the SDK, in any spelling, including the earlier install from GitHub (its "agent" name).
_PY_REQ_LINE = re.compile(r"(?im)^[ \t]*(redberry[-_](?:agent[-_])?sdk)\b[^\n]*$")
#: What an earlier connect wrote for the in-process hasher, which ``redberry_sdk`` no longer has.
_OLD_IMPORT = re.compile(r"(?m)^from redberry_sdk\.hasher import [^\n]*\n")
_OLD_REGISTER = re.compile(r"(?m)^register\(\)[ \t]*\n")
_OLD_COMMENT = re.compile(r"(?m)^# (Redberry: hashes, signs and stores each run[^\n]*|Its settings are the function's REDBERRY_\*"
                          r"[^\n]*|settings are the REDBERRY_\* app settings[^\n]*)\n")
_OLD_DECORATOR = re.compile(r"(?m)^([ \t]*)@with_redberry\([^\n]*\)[ \t]*$")
_OLD_WRAP = re.compile(r"with_redberry\((.+?),\s*agent_id=[^()]*\)")
_FLUSH = "redberry_sdk.flush_after"


def py_requirement_edit(reqs: Path) -> Edit | None:
    """``redberry-sdk[providers]`` in requirements.txt: added, or put in place of the earlier GitHub install."""
    req_text = reqs.read_text(encoding="utf-8") if reqs.exists() else None
    body = req_text or ""
    m = _PY_REQ_LINE.search(body)
    if m and m.group(1).lower().replace("_", "-") == "redberry-sdk":
        return None
    if m:
        return Edit(reqs, req_text, body[:m.start()] + PY_REQUIREMENT + body[m.end():],
                    f"{PY_REQUIREMENT} in place of the earlier install from GitHub")
    if body and not body.endswith("\n"):
        body += "\n"
    return Edit(reqs, req_text, body + PY_REQUIREMENT + "\n", f"add {PY_REQUIREMENT}")


def _py_import_end(tree) -> int:
    """The 0-based line after the module's leading docstring and imports."""
    import ast

    at = 0
    for i, node in enumerate(tree.body):
        if i == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            at = node.end_lineno or at
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            at = node.end_lineno or at
            continue
        break
    return at


def py_wire(text: str, agent: str, decorate: set[str], init_here: bool, where: str, notes: list[str],
            assigned: set[str] = frozenset()) -> tuple[str, list[str]]:
    """``text`` with Redberry wired in, and what changed:

    * the earlier hasher wiring (``from redberry_sdk.hasher import …``, ``register()``, ``with_redberry``)
      replaced, so connecting an app again moves it to the SDK;
    * ``@redberry_sdk.flush_after`` on each top-level function named in ``decorate`` (under its other
      decorators, so the platform's trigger decorators still see the handler), and ``x = flush_after(…)``
      for each name in ``assigned`` bound by an assignment;
    * with ``init_here``, ``import redberry_sdk`` and ``redberry_sdk.init(agent=…)`` once, after the imports.

    ``flush_after`` wraps a plain function: an ``async def`` is left alone and said in ``notes``."""
    import ast

    whys: list[str] = []
    migrated = _OLD_IMPORT.sub("", text)
    migrated = _OLD_REGISTER.sub("", migrated)
    migrated = _OLD_COMMENT.sub("", migrated)
    migrated = _OLD_DECORATOR.sub(lambda m: f"{m.group(1)}@{_FLUSH}", migrated)
    migrated = _OLD_WRAP.sub(lambda m: f"{_FLUSH}({m.group(1)})", migrated)
    if migrated != text:
        whys.append("replace the earlier redberry_sdk.hasher wiring")
        migrated = re.sub(r"\n{3,}(?=\S)", "\n\n\n", migrated)
    text = migrated
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    for fn in sorted((n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)), key=lambda n: n.lineno, reverse=True):
        for d in fn.decorator_list:                  # with_redberry wrapped coroutines; flush_after does not
            if ast.unparse(d) == _FLUSH and lines[d.lineno - 1].strip() == f"@{_FLUSH}":
                del lines[d.lineno - 1]
    if len(lines) != len(text.splitlines()):
        text = "".join(lines)
        tree = ast.parse(text)
    decorated = []
    for fn in sorted((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                     key=lambda n: n.lineno, reverse=True):
        if fn.name not in decorate or any(_FLUSH in ast.unparse(d) or "flush_after" == ast.unparse(d)
                                          for d in fn.decorator_list):
            continue
        if isinstance(fn, ast.AsyncFunctionDef):
            notes.append(f"{where}: {fn.name} is async, which redberry_sdk.flush_after does not wrap; its spans "
                         "are sent in the background (call redberry_sdk.flush() at its end to send them at once).")
            continue
        row = fn.lineno - 1                                          # the `def` line, under every decorator
        indent = lines[row][:len(lines[row]) - len(lines[row].lstrip())]
        lines.insert(row, f"{indent}@{_FLUSH}\n")
        decorated.append(fn.name)
    for node in sorted((n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name) and n.targets[0].id in assigned),
                       key=lambda n: n.lineno, reverse=True):
        src = ast.get_source_segment(text, node.value) or ""
        if "flush_after" in src:
            continue
        first, last = node.value.lineno - 1, (node.value.end_lineno or node.value.lineno) - 1
        head = lines[first][:node.value.col_offset]
        tail = lines[last][node.value.end_col_offset or 0:]
        lines[first:last + 1] = [f"{head}{_FLUSH}({src}){tail}"]
        decorated.append(node.targets[0].id)
    if decorated:
        whys.insert(0, "send the spans of " + ", ".join(reversed(decorated)) + " before it returns (flush_after)")
    has_import = re.search(r"(?m)^import redberry_sdk\b", text) is not None
    has_init = "redberry_sdk.init(" in text
    block = []
    if (decorated or (init_here and not has_init)) and not has_import:
        block.append("import redberry_sdk\n")
    if init_here and not has_init:
        block += ["\n", "# Redberry: records the agent's model and tool calls and sends them to the Redberry installation,\n",
                  "# which the SDK finds from where it runs (redberry connect).\n",
                  f"redberry_sdk.init(agent={json.dumps(agent)})\n"]
        whys.insert(0, "redberry_sdk.init() at module load")
    if block:
        at = _py_import_end(ast.parse("".join(lines)))
        if at < len(lines) and lines[at].strip():
            block.append("\n")
        lines[at:at] = block
    return "".join(lines), whys
