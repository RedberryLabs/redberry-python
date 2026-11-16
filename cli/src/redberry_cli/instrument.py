"""Put ``redberry_sdk.init()`` into the agent's code, for ``redberry connect``.

The SDK records an agent's model and tool calls once ``redberry_sdk.init()`` has run, before the agent
creates its LLM client. This finds where the agent creates one (``redberry_cli._scan``, a deterministic code scan,
no LLM) and, when that is exactly one Python file, proposes two lines at its top: after the module
docstring and any ``from __future__`` imports, before every other import. Pure functions over the
repository; ``connect`` shows the diff and asks before writing.

When the place is not clear (no client found, several files, already instrumented, not Python), it says
so and leaves the code alone: the caller prints the two lines for a person to place.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass
from pathlib import Path

LINES = ["import redberry_sdk  # Redberry: records the agent's model and tool calls (redberry connect)",
         "redberry_sdk.init()  # reads REDBERRY_AGENT_ID from the environment (.env)"]


@dataclass
class Plan:
    """Where to add the two lines, or why not."""

    path: Path | None
    reason: str
    new_text: str | None = None
    diff: str = ""


def plan(repo: Path) -> Plan:
    repo = repo.resolve()
    hits = [p for p in sorted(repo.rglob("*.py")) if _skip(p, repo) is False and "redberry_sdk.init(" in _read(p)]
    if hits:
        return Plan(None, f"already instrumented: {hits[0].relative_to(repo)}")
    from redberry_cli._scan import scan

    files = sorted({Path(m.file).resolve() for m in scan(repo).model_candidates
                    if m.file.endswith(".py") and not _skip(Path(m.file).resolve(), repo)})
    if not files:
        return Plan(None, "no LLM client found in the agent's Python code")
    if len(files) > 1:
        names = ", ".join(str(f.relative_to(repo)) for f in files[:5])
        return Plan(None, f"LLM clients are created in {len(files)} files ({names}): add the lines to the entry point")
    path = files[0]
    text = _read(path)
    try:
        at = _insert_line(text)
    except SyntaxError as e:
        return Plan(None, f"{path.relative_to(repo)} does not parse ({e.msg})")
    lines = text.splitlines(keepends=True)
    new_lines = [*lines[:at], *(line + "\n" for line in LINES), *(["\n"] if at < len(lines) else []), *lines[at:]]
    new_text = "".join(new_lines)
    rel = str(path.relative_to(repo))
    diff = "".join(difflib.unified_diff(lines, new_lines, f"a/{rel}", f"b/{rel}", n=2))
    return Plan(path, f"the LLM client is created in {rel}", new_text, diff)


def apply(p: Plan) -> None:
    assert p.path is not None and p.new_text is not None
    p.path.write_text(p.new_text, encoding="utf-8")


def _insert_line(text: str) -> int:
    """The 0-based line to insert at: after the module docstring and ``from __future__`` imports."""
    tree = ast.parse(text)
    at = 0
    for i, node in enumerate(tree.body):
        is_docstring = i == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
            and isinstance(node.value.value, str)
        is_future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        if not (is_docstring or is_future):
            break
        at = node.end_lineno or at
    lines = text.splitlines()
    while at == 0 and at < len(lines) and (lines[at].startswith("#!") or lines[at].startswith("# -*-")):
        at += 1                                   # keep a shebang or an encoding line first
    return at


def _skip(path: Path, repo: Path) -> bool:
    parts = path.relative_to(repo).parts if path.is_relative_to(repo) else ("..",)
    return any(p in (".venv", "venv", "node_modules", "site-packages", "__pycache__", "tests", "..") or p.startswith(".")
               for p in parts[:-1])


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ── temperature ──────────────────────────────────────────────────────────────────────────────

#: Pricing scores against the model's raw probabilities, which only mean what they say at exactly 1.0:
#: SD1B's gate refuses a call at any other temperature, or with none recorded.
TEMPERATURE = 1.0
_ANTHROPIC_CALLS = ("messages.create", "messages.stream")
_OPENAI_CALLS = ("chat.completions.create", "chat.completions.parse", "responses.create")


@dataclass
class TemperatureChange:
    path: Path
    line: int
    before: str           # "unset", or the value that was there ("0.2"); "variable" when it is not a literal
    changed: bool
    #: An Anthropic call on API 2026-10-21 (SDK 2.x): the provider fixes temperature at 1.0 and
    #: rejects any sent. Never changed; ``before`` other than "unset" is a temperature it must drop.
    provider_fixed: bool = False


def set_temperature(repo: Path, anthropic_v1: bool | None = None,
                    anthropic_fixed: bool | None = None) -> list[TemperatureChange]:
    """Make every model call the agent makes ask for temperature 1.0: added where none is set, any
    other literal replaced. A temperature held in a variable is reported, not changed (its value is not
    in the code). On the Anthropic SDK 1.x, which has no ``temperature`` argument, it goes in
    ``extra_body``.

    On the Anthropic SDK 2.x (``anthropic_fixed``) an Anthropic call is left alone: its API version,
    2026-10-21, rejects every sampling parameter and fixes temperature at 1.0, which the SDK records
    as ``provider_fixed_default``. Adding one would make every call fail with HTTP 400. A
    temperature such a call already sends is reported as ``rejected``, not changed.

    Writes the files; returns what it found at each call."""
    repo = repo.resolve()
    major = _anthropic_major() if anthropic_v1 is None or anthropic_fixed is None else None
    if anthropic_v1 is None:
        anthropic_v1 = major >= 1
    if anthropic_fixed is None:
        anthropic_fixed = major >= 2
    changes: list[TemperatureChange] = []
    for path in sorted(repo.rglob("*.py")):
        if _skip(path, repo):
            continue
        text = _read(path)
        if "create(" not in text and "stream(" not in text and "parse(" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))

        def at(row: int, col: int, lines=lines, offsets=offsets) -> int:
            return offsets[row - 1] + _char_col(lines[row - 1], col)

        edits: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind = _call_kind(node.func)
            if kind is None:
                continue
            kw = {k.arg: k for k in node.keywords if k.arg}
            temp = kw.get("temperature")
            extra = kw.get("extra_body")
            target = None                                  # the AST node holding the temperature value
            if temp is not None:
                target = temp.value
            elif extra is not None and isinstance(extra.value, ast.Dict):
                for k, v in zip(extra.value.keys, extra.value.values):
                    if isinstance(k, ast.Constant) and k.value == "temperature":
                        target = v
            if kind == "anthropic" and anthropic_fixed:
                # The provider fixes it; one sent is refused (HTTP 400). Never added, never changed.
                before = ("unset" if target is None else
                          repr(float(target.value)) if isinstance(target, ast.Constant)
                          and isinstance(target.value, (int, float)) else "variable")
                changes.append(TemperatureChange(path, node.lineno, before, False,
                                                 provider_fixed=True))
                continue
            if target is not None:
                if isinstance(target, ast.Constant) and isinstance(target.value, (int, float)):
                    before = repr(float(target.value))
                    if float(target.value) != TEMPERATURE:
                        edits.append((at(target.lineno, target.col_offset), at(target.end_lineno, target.end_col_offset),
                                      "1.0"))
                    changes.append(TemperatureChange(path, node.lineno, before, float(target.value) != TEMPERATURE))
                else:
                    changes.append(TemperatureChange(path, node.lineno, "variable", False))
                continue
            # None set: add it.
            use_extra = kind == "anthropic" and anthropic_v1
            if use_extra and extra is not None:
                if not isinstance(extra.value, ast.Dict):
                    changes.append(TemperatureChange(path, node.lineno, "variable", False))
                    continue
                brace = at(extra.value.lineno, extra.value.col_offset) + 1
                sep = ", " if extra.value.keys else ""
                edits.append((brace, brace, f'"temperature": {TEMPERATURE}{sep}'))
            else:
                arg = f'extra_body={{"temperature": {TEMPERATURE}}}' if use_extra else f"temperature={TEMPERATURE}"
                edits.append(_add_keyword(node, lines, at, arg))
            changes.append(TemperatureChange(path, node.lineno, "unset", True))
        if edits:
            for start, end, new in sorted(edits, reverse=True):
                text = text[:start] + new + text[end:]
            path.write_text(text, encoding="utf-8")
    return changes


def _call_kind(func: ast.expr) -> str | None:
    """"anthropic" or "openai" when ``func`` is a provider's model call (``client.messages.create``)."""
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    dotted = ".".join(reversed(parts))
    if any(dotted == c or dotted.endswith("." + c) for c in _ANTHROPIC_CALLS):
        return "anthropic"
    if any(dotted == c or dotted.endswith("." + c) for c in _OPENAI_CALLS):
        return "openai"
    return None


def _add_keyword(node: ast.Call, lines: list[str], at, arg: str) -> tuple[int, int, str]:
    """An edit adding ``arg`` as the call's first keyword: on its own line when the arguments are."""
    args = [*node.args, *node.keywords]
    first = min(args, key=lambda a: (a.lineno, a.col_offset)) if args else None
    if first is not None and first.lineno > node.func.end_lineno:
        indent = lines[first.lineno - 1][:len(lines[first.lineno - 1]) - len(lines[first.lineno - 1].lstrip())]
        pos = at(first.lineno, 0)
        return pos, pos, f"{indent}{arg},\n"
    if first is not None:
        pos = at(first.lineno, first.col_offset)
        return pos, pos, f"{arg}, "
    pos = at(node.end_lineno, node.end_col_offset) - 1                    # just inside the ")"
    return pos, pos, arg


def _char_col(line: str, byte_col: int) -> int:
    """AST column offsets are UTF-8 byte offsets; a str index is a character offset."""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


def _anthropic_major() -> int:
    try:
        from importlib.metadata import version

        return int(version("anthropic").split(".")[0])
    except Exception:  # noqa: BLE001 -- not installed: extra_body works on every version
        return 1
