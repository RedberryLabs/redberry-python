"""A deterministic scan of an agent's code, with no LLM and nothing sent anywhere.

Used by ``redberry connect`` to check that a folder holds an agent before anything is changed, and to find
where the agent creates its LLM client (``redberry_cli.instrument``) and which functions reach a model call
(``redberry_cli.azure_functions``). It reports:

  - the framework, from pyproject.toml / requirements.txt / package.json;
  - tool candidates (framework-specific decorator and class patterns) and tools written as literals;
  - model clients (API client instantiations), in Python and, through ``_ts_tools``, the AI SDK's calls;
  - the files most likely to hold the agent's logic.

Every finding carries its file and line, so a caller can show where it came from.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Packages that indicate a known agent framework
_FRAMEWORK_PACKAGES: dict[str, str] = {
    "langgraph": "langgraph",
    "langchain": "langchain",
    "langchain-core": "langchain",
    "langchain-openai": "langchain",
    "openai-agents": "openai_agents",
    "openai_agents": "openai_agents",
    # Not "agents": these keys are DISTRIBUTION names read from requirement files, and the
    # distribution called `agents` is not the OpenAI Agents SDK (`openai-agents`). Its import name
    # is `agents`, which is what a project's own package is often called.
    "autogen": "autogen",
    "autogen-agentchat": "autogen",
    "llama-index": "llamaindex",
    "llama_index": "llamaindex",
}

# Patterns that identify tool definitions (framework-specific)
_TOOL_PATTERNS: dict[str, list[str]] = {
    "langchain": [r"@tool\b", r"class\s+\w+\s*\(BaseTool\)", r"StructuredTool\.from_function"],
    "openai_agents": [r"@function_tool\b", r"FunctionTool\("],
    "autogen": [r"@register_for_llm", r"register_function"],
    "llamaindex": [r"FunctionTool\.from_defaults", r"@tool\b"],
    "custom": [r"def\s+\w+.*tool", r"tools\s*=\s*\["],
}

# Patterns that identify model client instantiation
_MODEL_PATTERNS: list[str] = [
    r"ChatOpenAI\(",
    r"OpenAI\(",
    r"AzureChatOpenAI\(",
    r"anthropic\.Anthropic\(",
    r"anthropic\.AsyncAnthropic\(",
    r"Anthropic\(",
    r"ChatAnthropic\(",
    r"genai\.GenerativeModel\(",
    r"ChatGoogleGenerativeAI\(",
]


@dataclass
class ToolCandidate:
    name: str | None
    file: str
    line: int
    snippet: str
    pattern_matched: str


@dataclass
class StaticTool:
    """A tool whose definition is a literal in the code: the dict an agent hands its model (Anthropic's
    ``{"name", "description", "input_schema"}``, OpenAI's ``{"type": "function", "function": {...,
    "parameters"}}``). Read from the syntax tree, so it is exact and the same on every run."""

    name: str
    description: str | None
    parameters_schema: dict[str, Any] | None
    file: str
    line: int


@dataclass
class ModelCandidate:
    file: str
    line: int
    snippet: str
    provider_hint: str


@dataclass
class ScanResult:
    repo_root: str
    framework: str           # "langgraph" | "openai_agents" | "autogen" | "llamaindex" | "custom" | "unknown"
    framework_version: str | None
    relevant_files: list[str]     # the files most likely to hold the agent's logic, first
    tool_candidates: list[ToolCandidate]
    model_candidates: list[ModelCandidate]
    requirements: dict[str, str]  # package -> version found in requirements files
    notes: list[str] = field(default_factory=list)
    static_tools: list[StaticTool] = field(default_factory=list)


def scan(repo_root: str | Path) -> ScanResult:
    root = Path(repo_root).resolve()
    logger.info("Scan starting on %s", root)

    requirements = _scan_requirements(root)
    framework, framework_version = _detect_framework(requirements)
    logger.info("Detected framework: %s (version: %s)", framework, framework_version)

    py_files = _collect_python_files(root)
    relevant_files = _rank_relevant_files(py_files, framework, root)

    tool_candidates = _first_of_each(_scan_tools(py_files, framework))
    static_tools = _scan_static_tools(py_files)
    model_candidates = _scan_models(py_files)

    notes = []
    ts_files, ts_tools, ts_models = _scan_typescript(root)
    if ts_tools or ts_models:
        # A TypeScript / JavaScript agent (Vercel AI SDK): its tools are read exactly, as the
        # Python literals are, and the files that declare them or call the model come first.
        static_tools += [t for t in ts_tools if t.name not in {s.name for s in static_tools}]
        model_candidates += ts_models
        first = {t.file for t in ts_tools} | {m.file for m in ts_models}
        relevant_files = [Path(f) for f in sorted(first)] + \
            [f for f in ts_files if str(f) not in first] + relevant_files
        if framework == "unknown":
            framework, framework_version = "custom", _npm_version(root, "ai")
        notes.append(f"TypeScript/JavaScript agent: {len(ts_tools)} AI SDK tool(s) and "
                     f"{len(ts_models)} model call(s) read statically.")
    if framework == "unknown":
        notes.append("Framework could not be detected from requirements.")
    if not tool_candidates and not static_tools:
        notes.append("No tool definitions found by pattern scan.")

    return ScanResult(
        repo_root=str(root),
        framework=framework,
        framework_version=framework_version,
        relevant_files=[str(f) for f in relevant_files[:30]],
        tool_candidates=tool_candidates,
        model_candidates=model_candidates,
        requirements=requirements,
        notes=notes,
        static_tools=static_tools,
    )


#: The scan's earlier name, kept so callers written against it still read.
run_bootstrap = scan


def _scan_typescript(root: Path) -> tuple[list[Path], list[StaticTool], list[ModelCandidate]]:
    """The AI SDK's ``tool({...})`` declarations and model calls (``_ts_tools``), as scan
    candidates. Without this a Next.js agent had no tools: the scan above reads Python."""
    from redberry_cli import _ts_tools as ts

    files = ts.source_files(root)
    if not files:
        return [], [], []
    tools = [StaticTool(name=t.name, description=t.description, parameters_schema=t.parameters_schema,
                        file=str(root / t.file), line=t.line)
             for t in ts.scan_tools(files, root)]
    models = []
    for c in ts.scan_model_calls(files):
        try:
            text = c.file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        models.append(ModelCandidate(file=str(c.file), line=c.line,
                                     snippet=text[c.open_brace:c.close_brace + 1][:300],
                                     provider_hint="unknown"))
    return files, tools, models


def _npm_version(root: Path, package: str) -> str | None:
    try:
        doc = json.loads((root / "package.json").read_text())
    except (OSError, ValueError):
        return None
    for key in ("dependencies", "devDependencies"):
        spec = (doc.get(key) or {}).get(package)
        if isinstance(spec, str):
            return spec.lstrip("^~>=<") or None
    return None


# ------------------------------------------------------------------
# Requirements scanning
# ------------------------------------------------------------------

def _scan_requirements(root: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for candidate in [
        root / "requirements.txt",
        root / "requirements-base.txt",
        root / "pyproject.toml",
        root / "setup.py",
        root / "setup.cfg",
        root / "Pipfile",
    ]:
        if candidate.exists():
            _parse_requirements_file(candidate, packages)
    return packages


def _parse_requirements_file(path: Path, packages: dict[str, str]) -> None:
    text = path.read_text(errors="replace")
    if path.name == "pyproject.toml":
        # Very simple TOML parser for dependencies arrays
        for line in text.splitlines():
            line = line.strip().strip('"').strip("'").strip(",")
            m = re.match(r'^([\w\-]+)\s*[>=<~^!]*\s*([\d.]+)?', line)
            if m:
                packages[m.group(1).lower()] = m.group(2) or ""
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                m = re.match(r'^([\w\-]+)\s*[>=<~^!]*\s*([\d.]+)?', line)
                if m:
                    packages[m.group(1).lower()] = m.group(2) or ""


def _detect_framework(packages: dict[str, str]) -> tuple[str, str | None]:
    for pkg, framework in _FRAMEWORK_PACKAGES.items():
        if pkg.lower() in packages:
            return framework, packages.get(pkg.lower())
    return "unknown", None


# ------------------------------------------------------------------
# Python file collection
# ------------------------------------------------------------------

def _collect_python_files(root: Path) -> list[Path]:
    skip_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", "dist", "build"}
    result: list[Path] = []
    for py in root.rglob("*.py"):
        if not any(part in skip_dirs for part in py.parts):
            result.append(py)
    return result


def _rank_relevant_files(files: list[Path], framework: str, root: Path) -> list[Path]:
    """Rank files by how likely they are to contain agent logic."""
    def score(f: Path) -> int:
        name = f.name.lower()
        s = 0
        if any(kw in name for kw in ("agent", "tool", "prompt", "chain", "graph", "task")):
            s += 10
        if "test" in name or "conftest" in name:
            s -= 5
        if framework == "langgraph" and "graph" in name:
            s += 5
        if framework == "openai_agents" and "agent" in name:
            s += 5
        return s

    return sorted(files, key=score, reverse=True)


# ------------------------------------------------------------------
# Pattern scanning
# ------------------------------------------------------------------

def _scan_tools(files: list[Path], framework: str) -> list[ToolCandidate]:
    # Every framework's patterns, whatever the requirements files say: an Agents SDK agent with
    # no requirements.txt or pyproject.toml is detected as "unknown", and its @function_tool
    # tools went unseen. The framework detected only orders the patterns.
    patterns = list(dict.fromkeys(_TOOL_PATTERNS.get(framework, []) +
                                  [p for ps in _TOOL_PATTERNS.values() for p in ps]))
    candidates: list[ToolCandidate] = []
    compiled = [(p, re.compile(p)) for p in patterns]

    for path in files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for pattern_str, rx in compiled:
            for m in rx.finditer(text):
                lineno = text[:m.start()].count("\n") + 1
                snippet_lines = text.splitlines()[max(0, lineno - 1):lineno + 2]
                snippet = "\n".join(snippet_lines)[:300]
                # Try to extract the function/class name from context
                name = _extract_name_near(text, m.start())
                candidates.append(ToolCandidate(
                    name=name,
                    file=str(path),
                    line=lineno,
                    snippet=snippet,
                    pattern_matched=pattern_str,
                ))

    return _deduplicate_candidates(candidates)


def _first_of_each(candidates: list[ToolCandidate]) -> list[ToolCandidate]:
    """One candidate per (name, file), so each tool is reported once."""
    seen: set[tuple[str | None, str]] = set()
    out = []
    for c in candidates:
        key = (c.name, c.file)
        if c.name is not None and key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _scan_static_tools(files: list[Path]) -> list[StaticTool]:
    """Tool definitions written as literals, from each file's syntax tree (no model call). An agent
    that calls its provider directly keeps its tools as such dicts, often in a list far down a module
    (a ``TOOL_SCHEMAS`` list, say), where a reader of file heads never reaches them."""
    found: dict[str, StaticTool] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            literal = _literal_dict(node, consts)
            if literal is None:
                continue
            if literal.get("type") == "function" and isinstance(literal.get("function"), dict):
                literal = literal["function"]                      # OpenAI's wrapper
            name = literal.get("name")
            schema = literal.get("input_schema", literal.get("parameters"))
            if not (isinstance(name, str) and name and isinstance(schema, dict)):
                continue
            if schema.get("type") not in (None, "object") or name in found:
                continue
            description = literal.get("description")
            found[name] = StaticTool(name=name, description=description if isinstance(description, str) else None,
                                     parameters_schema=schema, file=str(path), line=node.lineno)
    return list(found.values())


#: Where a schema records the values it could not read: {key: the source text}.
UNRESOLVED_KEY = "x-redberry-unresolved"
_UNREADABLE = object()


def _module_constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level `NAME = value` assignments: what a schema's `"enum": _PRACTICE_AREAS` names."""
    out: dict[str, ast.expr] = {}
    for st in tree.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            out[st.targets[0].id] = st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            out[st.target.id] = st.value
    return out


#: JSON Schema maps whose keys are names (of arguments, definitions), not keywords: a note
#: about an unreadable value must never sit beside those names as if it were one.
_NAME_MAPS = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}


def _literal(node: ast.AST, consts: dict[str, ast.expr], seen: tuple[str, ...] = (), *,
             names: bool = False) -> Any:
    """A literal's value, reading a module constant it names; `_UNREADABLE` otherwise. A
    collection with one unreadable item is unreadable as a whole: half an enum is not the enum.
    `names`: the node is the value of a name-keyed map (`properties`)."""
    if isinstance(node, ast.Dict):
        d = _literal_dict(node, consts, seen, names=names)
        return _UNREADABLE if d is None else d
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_literal(e, consts, seen) for e in node.elts]
        if any(x is _UNREADABLE for x in items):
            return _UNREADABLE
        return list(items)                     # a schema is JSON: a tuple of values is a list
    if isinstance(node, ast.Name) and node.id in consts and node.id not in seen:
        return _literal(consts[node.id], consts, seen + (node.id,), names=names)
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _UNREADABLE


def _literal_dict(node: ast.Dict, consts: dict[str, ast.expr] | None = None,
                  seen: tuple[str, ...] = (), *, names: bool = False) -> dict[str, Any] | None:
    """The dict a literal spells, read value by value (one `"enum": _PRACTICE_AREAS`
    used to drop the whole tool). A value or a `**spread`
    naming a module constant in the same file is read through it. A value that cannot be read:
    - in a map whose keys are names (`names`: `properties`, `$defs`, ...) keeps its key, its
      value becoming `{UNRESOLVED_KEY: "<source>"}`, an unconstrained schema: the argument is
      still an argument, and no note is mistaken for one;
    - anywhere else (a keyword: `"enum": sorted(T)`) is left out, its source recorded under
      `UNRESOLVED_KEY` beside the keywords, where JSON Schema allows an extension keyword.
    None: not a string-keyed dict, or a spread that cannot be read."""
    consts = consts or {}
    out: dict[str, Any] = {}
    unresolved: dict[str, str] = {}
    for k, v in zip(node.keys, node.values):
        if k is None:                                              # {**BASE, ...}
            spread = _literal(v, consts, seen)
            if not isinstance(spread, dict):
                return None
            out.update(spread)
            continue
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            return None
        value = _literal(v, consts, seen, names=k.value in _NAME_MAPS and not names)
        if value is not _UNREADABLE:
            out[k.value] = value
        elif names:
            out[k.value] = {UNRESOLVED_KEY: ast.unparse(v)[:120]}
        else:
            unresolved[k.value] = ast.unparse(v)[:120]
    if unresolved:
        out[UNRESOLVED_KEY] = unresolved
    return out


def _scan_models(files: list[Path]) -> list[ModelCandidate]:
    candidates: list[ModelCandidate] = []
    compiled = [(p, re.compile(p)) for p in _MODEL_PATTERNS]
    provider_map = {
        "OpenAI": "openai", "AzureChatOpenAI": "azure_openai",
        "ChatOpenAI": "openai", "Anthropic": "anthropic",
        "ChatAnthropic": "anthropic", "ChatGoogleGenerativeAI": "google",
        "genai": "google",
    }

    for path in files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for pattern_str, rx in compiled:
            for m in rx.finditer(text):
                lineno = text[:m.start()].count("\n") + 1
                snippet_lines = text.splitlines()[max(0, lineno - 1):lineno + 2]
                snippet = "\n".join(snippet_lines)[:300]
                provider = "unknown"
                for hint, prov in provider_map.items():
                    if hint in m.group():
                        provider = prov
                        break
                candidates.append(ModelCandidate(
                    file=str(path), line=lineno, snippet=snippet, provider_hint=provider
                ))

    return candidates


def _extract_name_near(text: str, pos: int) -> str | None:
    """Try to find the function/class name defined near this position."""
    window = text[max(0, pos - 50):pos + 200]
    m = re.search(r'def\s+(\w+)\s*\(', window)
    if m:
        return m.group(1)
    m = re.search(r'class\s+(\w+)\s*[\(:]', window)
    if m:
        return m.group(1)
    return None



def _deduplicate_candidates(candidates: list[ToolCandidate]) -> list[ToolCandidate]:
    seen: set[tuple] = set()
    result: list[ToolCandidate] = []
    for c in candidates:
        key = (c.file, c.line)
        if key not in seen:
            seen.add(key)
            result.append(c)
    return result
