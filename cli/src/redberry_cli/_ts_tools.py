"""A small static reader for Vercel AI SDK agents written in TypeScript / JavaScript.

The Python scan (``redberry_cli._scan``) reads Python only, so a Next.js agent's tools were invisible to
it. This reads the AI SDK's own way of declaring a tool, without running anything::

    lookup_customer: tool({
      description: 'Find a customer by email or customer ID.',
      inputSchema: z.object({ email: z.string().optional(), customer_id: z.string() }),
      execute: async (...) => ...,
    }),

(``const lookupCustomer = tool({...})`` too; ``parameters`` is AI SDK 4's name for
``inputSchema``.) The Zod schema is turned into JSON Schema for the common constructors
(``object``, ``string``, ``number``, ``boolean``, ``enum``, ``literal``, ``array``, ``union``,
``optional``, ``nullable``, ``describe``); anything else is kept as its source text in
``parameters_source`` with ``parameters_schema`` None, never guessed.

It also finds the agent's model calls (``streamText`` / ``generateText`` / ``ToolLoopAgent``) and
the ``temperature`` each sets, which pricing needs at exactly 1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKIP_DIRS = frozenset({"node_modules", ".next", ".git", ".vercel", "dist", "build", "out", ".turbo",
                       "coverage"})
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", ".cjs", ".cts")
MODEL_CALLS = ("streamText", "generateText", "streamObject", "generateObject", "ToolLoopAgent",
               "Experimental_Agent")


@dataclass
class TsTool:
    name: str
    description: str | None
    parameters_schema: dict[str, Any] | None
    parameters_source: str | None
    file: str
    line: int


@dataclass
class ModelCall:
    """A ``streamText({...})``-style call and its options object's span in the file."""
    kind: str
    file: Path
    line: int
    open_brace: int          # index of the options object's "{"
    close_brace: int         # index of its "}"
    temperature: str | None  # the literal source of `temperature:`, or None when absent
    has_spread: bool = False


def source_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.suffix in SOURCE_SUFFIXES and p.is_file() and not p.name.endswith(".d.ts") \
                and not any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Lexing helpers: strings and comments are skipped when matching brackets
# ---------------------------------------------------------------------------


def _skip_string(text: str, i: int) -> int:
    """``text[i]`` opens a string; the index just past its end."""
    quote = text[i]
    i += 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if quote == "`" and c == "$" and text[i + 1:i + 2] == "{":
            i = match_bracket(text, i + 1) + 1
            continue
        if c == quote:
            return i + 1
        i += 1
    return i


def match_bracket(text: str, i: int) -> int:
    """``text[i]`` is an opening bracket; the index of its matching close (or len(text) - 1)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[text[i]]]
    i += 1
    while i < len(text):
        c = text[i]
        if c in "'\"`":
            i = _skip_string(text, i)
            continue
        if c == "/" and text[i + 1:i + 2] == "/":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        if c == "/" and text[i + 1:i + 2] == "*":
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if stack and c == stack[-1]:
                stack.pop()
                if not stack:
                    return i
        i += 1
    return len(text) - 1


def _top_level_entries(body: str) -> list[tuple[str | None, str, int]]:
    """An object literal's body split at top-level commas: (key or None for a spread, value, offset)."""
    entries: list[tuple[str | None, str, int]] = []
    start, i = 0, 0
    while i <= len(body):
        c = body[i] if i < len(body) else ","
        if c in "'\"`" and i < len(body):
            i = _skip_string(body, i)
            continue
        if c in "([{" and i < len(body):
            i = match_bracket(body, i) + 1
            continue
        if c == "/" and body[i + 1:i + 2] == "/":
            nl = body.find("\n", i)
            i = len(body) if nl < 0 else nl
            continue
        if c == ",":
            chunk = body[start:i]
            stripped = re.sub(r"^\s*(//[^\n]*\n\s*)*", "", chunk)
            if stripped.strip():
                lead = len(chunk) - len(stripped)
                if stripped.lstrip().startswith("..."):
                    entries.append((None, stripped.strip()[3:], start + lead))
                else:
                    m = re.match(r"\s*(?:['\"]([^'\"]+)['\"]|([A-Za-z_$][\w$]*))\s*(:)?", stripped)
                    if m:
                        key = m.group(1) or m.group(2)
                        value = stripped[m.end():].strip() if m.group(3) else key   # shorthand
                        entries.append((key, value, start + lead))
            start = i + 1
        i += 1
    return entries


def _string_literal(src: str) -> str | None:
    """The value of a string literal (or of literals joined with ``+``); None otherwise."""
    parts = []
    rest = src.strip()
    while rest:
        m = re.match(r"""(['"])((?:\\.|(?!\1).)*)\1|`((?:\\.|[^`$])*)`""", rest, re.S)
        if not m:
            return None
        raw = m.group(2) if m.group(1) else m.group(3)
        parts.append(re.sub(r"\\(.)", lambda x: {"n": "\n", "t": "\t"}.get(x.group(1), x.group(1)), raw))
        rest = rest[m.end():].strip()
        if rest.startswith("+"):
            rest = rest[1:].strip()
        elif rest:
            return None
    return "".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Zod -> JSON Schema (the common subset)
# ---------------------------------------------------------------------------


class _Unparsed(Exception):
    pass


def _split_args(src: str) -> list[str]:
    args, depth, start, i = [], 0, 0, 0
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            i = _skip_string(src, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(src[start:i].strip())
            start = i + 1
        i += 1
    if src[start:].strip():
        args.append(src[start:].strip())
    return args


def _literal_value(src: str) -> Any:
    s = _string_literal(src)
    if s is not None:
        return s
    if re.fullmatch(r"-?\d+(\.\d+)?", src.strip()):
        return float(src) if "." in src else int(src)
    if src.strip() in ("true", "false"):
        return src.strip() == "true"
    if src.strip() == "null":
        return None
    raise _Unparsed(src)


def zod_to_json_schema(src: str) -> tuple[dict[str, Any], bool]:
    """(JSON Schema, optional?) for a Zod expression. Raises ``_Unparsed`` on anything else."""
    src = src.strip()
    m = re.match(r"z\s*\.\s*(\w+)\s*\(", src)
    if not m:
        raise _Unparsed(src)
    kind = m.group(1)
    close = match_bracket(src, m.end() - 1)
    args = src[m.end():close]
    rest = src[close + 1:]
    optional = False
    if kind == "object":
        body = args.strip()
        if not (body.startswith("{") and body.endswith("}")):
            raise _Unparsed(src)
        props, required = {}, []
        for key, value, _ in _top_level_entries(body[1:-1]):
            if key is None:
                raise _Unparsed(src)
            schema, opt = zod_to_json_schema(value)
            props[key] = schema
            if not opt:
                required.append(key)
        schema: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        schema["additionalProperties"] = False
    elif kind in ("string", "number", "boolean"):
        schema = {"type": kind}
    elif kind == "bigint":
        schema = {"type": "integer"}
    elif kind in ("any", "unknown"):
        schema = {}
    elif kind == "null":
        schema = {"type": "null"}
    elif kind == "enum":
        arr = args.strip()
        if not (arr.startswith("[") and arr.endswith("]")):
            raise _Unparsed(src)
        values = [_literal_value(a) for a in _split_args(arr[1:-1])]
        schema = {"type": "string", "enum": values}
    elif kind == "literal":
        schema = {"const": _literal_value(args)}
    elif kind == "array":
        inner, _ = zod_to_json_schema(args)
        schema = {"type": "array", "items": inner}
    elif kind == "union":
        arr = args.strip()
        if not (arr.startswith("[") and arr.endswith("]")):
            raise _Unparsed(src)
        schema = {"anyOf": [zod_to_json_schema(a)[0] for a in _split_args(arr[1:-1])]}
    else:
        raise _Unparsed(src)

    # Method chain: .optional() .nullable() .describe('...') .int() .min(..) ...
    while rest.strip():
        mm = re.match(r"\s*\.\s*(\w+)\s*\(", rest)
        if not mm:
            raise _Unparsed(src)
        c = match_bracket(rest, mm.end() - 1)
        method, arg = mm.group(1), rest[mm.end():c]
        rest = rest[c + 1:]
        if method in ("optional", "nullish"):
            optional = True
        if method in ("nullable", "nullish"):
            schema = {"anyOf": [schema, {"type": "null"}]}
        elif method == "describe":
            text = _string_literal(arg)
            if text is None:
                raise _Unparsed(src)
            schema["description"] = text
        elif method == "int":
            schema["type"] = "integer"
        elif method == "default":
            optional = True
        elif method in ("min", "max", "length", "email", "url", "uuid", "regex", "positive",
                        "nonnegative", "negative", "nonpositive", "trim", "toLowerCase", "datetime",
                        "optional", "nullish", "strict", "passthrough"):
            continue                  # constraints not needed to name and type an argument
        else:
            raise _Unparsed(src)
    return schema, optional


# ---------------------------------------------------------------------------
# Tools and model calls
# ---------------------------------------------------------------------------

_TOOL_RE = re.compile(r"(?:(?:const|let|var)\s+)?(?:['\"]([\w$-]+)['\"]|([A-Za-z_$][\w$]*))\s*[:=]\s*"
                      r"tool\s*(?:<[^>]*>)?\s*\(\s*\{")


def scan_tools(files: list[Path], root: Path | None = None) -> list[TsTool]:
    tools: list[TsTool] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "tool(" not in text.replace("tool (", "tool("):
            continue
        for m in _TOOL_RE.finditer(text):
            name = m.group(1) or m.group(2)
            brace = m.end() - 1
            body = text[brace + 1:match_bracket(text, brace)]
            entries = {k: v for k, v, _ in _top_level_entries(body) if k}
            description = _string_literal(entries["description"]) if "description" in entries else None
            schema_src = entries.get("inputSchema") or entries.get("parameters")
            schema = None
            if schema_src:
                try:
                    schema, _ = zod_to_json_schema(schema_src)
                except (_Unparsed, IndexError, ValueError):
                    schema = None
            tools.append(TsTool(
                name=name, description=description, parameters_schema=schema,
                parameters_source=schema_src.strip() if schema_src else None,
                file=str(f.relative_to(root) if root else f), line=text.count("\n", 0, m.start()) + 1))
    return tools


_CALL_RE = re.compile(r"(?<![\w$.])(?:new\s+)?(" + "|".join(MODEL_CALLS) + r")\s*(?:<[^>]*>)?\s*\(\s*\{")


def _imports_ai(text: str) -> bool:
    return bool(re.search(r"""from\s+['"]ai['"]|require\(\s*['"]ai['"]\s*\)""", text))


def scan_model_calls(files: list[Path]) -> list[ModelCall]:
    calls: list[ModelCall] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _imports_ai(text):
            continue
        for m in _CALL_RE.finditer(text):
            brace = m.end() - 1
            close = match_bracket(text, brace)
            entries = _top_level_entries(text[brace + 1:close])
            temp = next((v for k, v, _ in entries if k == "temperature"), None)
            calls.append(ModelCall(kind=m.group(1), file=f, line=text.count("\n", 0, m.start()) + 1,
                                   open_brace=brace, close_brace=close, temperature=temp,
                                   has_spread=any(k is None for k, _, _ in entries)))
    return calls


def set_temperature_one(text: str, call: ModelCall) -> tuple[str, str]:
    """The file text with ``temperature: 1`` in the call's options, and what changed:
    "inserted", "replaced <old>", "kept" (already 1), or "variable" (not a literal: left alone)."""
    body = text[call.open_brace + 1:call.close_brace]
    for key, value, offset in _top_level_entries(body):
        if key != "temperature":
            continue
        if re.fullmatch(r"1(\.0*)?", value.strip()):
            return text, "kept"
        if not re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
            return text, "variable"
        start = call.open_brace + 1 + offset
        seg = text[start:call.close_brace]
        mm = re.search(r"temperature\s*:\s*" + re.escape(value.strip()), seg)
        if not mm:
            return text, "variable"
        new = text[:start + mm.start()] + "temperature: 1" + text[start + mm.end():]
        return new, f"replaced {value.strip()}"
    # Insert as the first property, matching the indentation of the next line when there is one.
    after = text[call.open_brace + 1:]
    nl = re.match(r"[ \t]*\n([ \t]*)", after)
    if nl:
        ins = f"\n{nl.group(1)}temperature: 1,"
    else:
        ins = " temperature: 1,"
    return text[:call.open_brace + 1] + ins + text[call.open_brace + 1:], "inserted"


__all__ = ["ModelCall", "TsTool", "match_bracket", "scan_model_calls", "scan_tools",
           "set_temperature_one", "source_files", "zod_to_json_schema"]
