"""``redberry_cli._scan``: the deterministic scan of an agent's code that connect runs before it changes anything.

It finds every tool an agent exposes, including a tool whose schema names a module constant
(``"enum": _PRACTICE_AREAS``) and decorator-defined tools in a repository without a requirements file; the
model clients it creates; and a TypeScript agent's AI SDK tools. Nothing is sent anywhere.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from redberry_cli import _scan
from redberry_cli._scan import UNRESOLVED_KEY, _detect_framework, _parse_requirements_file, scan


def _names(root) -> set[str]:
    b = scan(root)
    return {t.name for t in b.static_tools} | {c.name for c in b.tool_candidates if c.name}


def _agent(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return tmp_path


def test_a_schema_naming_a_constant_keeps_its_tool_and_reads_the_constant(tmp_path):
    root = _agent(tmp_path, {"agent.py": '''
        AREAS = ("employment", "property")
        TOOLS = [{"type": "function", "function": {
            "name": "open_matter",
            "parameters": {"type": "object", "properties": {
                "practice_area": {"type": "string", "enum": AREAS}}}}}]
    '''})
    [t] = scan(root).static_tools
    assert t.name == "open_matter"
    assert t.parameters_schema["properties"]["practice_area"]["enum"] == ["employment", "property"]


def test_a_value_it_cannot_read_is_marked_not_the_whole_tool_lost(tmp_path):
    root = _agent(tmp_path, {"agent.py": '''
        from catalogue import TEMPLATES
        TOOLS = [{"name": "send_letter", "input_schema": {"type": "object", "properties": {
            "template_id": {"type": "string", "enum": sorted(TEMPLATES)}}}}]
    '''})
    [t] = scan(root).static_tools
    prop = t.parameters_schema["properties"]["template_id"]
    assert "enum" not in prop and prop[UNRESOLVED_KEY] == {"enum": "sorted(TEMPLATES)"}


def test_half_an_enum_is_not_the_enum(tmp_path):
    root = _agent(tmp_path, {"agent.py": '''
        TOOLS = [{"name": "t", "parameters": {"type": "object", "properties": {
            "x": {"enum": ["a", OTHER]}}}}]
    '''})
    [t] = scan(root).static_tools
    assert t.parameters_schema["properties"]["x"] == {UNRESOLVED_KEY: {"enum": "['a', OTHER]"}}


@pytest.mark.parametrize("source", [
    "from agents import function_tool\n\n\n@function_tool\ndef close_account(account_id: str) -> str:\n"
    "    return backend.close(account_id)\n",
    "from langchain_core.tools import tool\n\n\n@tool\ndef close_account(account_id: str) -> str:\n"
    "    return backend.close(account_id)\n",
])
def test_decorated_tools_are_found_without_a_requirements_file(tmp_path, source):
    """Framework detection reads requirements files; tool discovery no longer depends on it."""
    root = _agent(tmp_path, {"agent.py": source})
    b = scan(root)
    assert b.framework == "unknown" and "close_account" in _names(root)


def test_an_unreadable_argument_schema_keeps_its_argument(tmp_path):
    """A note about an unreadable value must never sit in `properties` as if it
    were an argument, and the arguments it describes must not vanish. """
    root = _agent(tmp_path, {"agent.py": '''
        BASE = {"type": "string"}
        AREAS = ["family", "criminal"]
        TOOLS = [{"type": "function", "function": {"name": "file_claim", "description": "x",
          "parameters": {"type": "object", "properties": {
              "amount": {**BASE, "description": "amt"},
              "area": {"type": "string", "enum": AREAS},
              "who": make_schema()},
            "required": ["amount", "area", "who"]}}}]
    '''})
    [t] = scan(root).static_tools
    props = t.parameters_schema["properties"]
    assert set(props) == {"amount", "area", "who"}
    assert not any(k.startswith("x-") for k in props)
    assert props["amount"] == {"type": "string", "description": "amt"}      # the spread, read through
    assert props["area"]["enum"] == ["family", "criminal"]
    assert props["who"] == {UNRESOLVED_KEY: "make_schema()"}                 # an unconstrained schema


def test_definitions_keep_their_names_too(tmp_path):
    root = _agent(tmp_path, {"agent.py": '''
        TOOLS = [{"name": "t", "input_schema": {"type": "object",
            "$defs": {"Payee": build_payee()}, "properties": {"p": {"$ref": "#/$defs/Payee"}}}}]
    '''})
    [t] = scan(root).static_tools
    assert t.parameters_schema["$defs"] == {"Payee": {UNRESOLVED_KEY: "build_payee()"}}


def test_each_tool_is_one_candidate(tmp_path):
    """Two patterns matching the same tool: it is reported once."""
    root = _agent(tmp_path, {"agent.py": '''
        from agents import function_tool
        @function_tool
        def refund(order_id: str) -> str:
            return backend.refund(order_id)
    '''})
    names = [c.name for c in scan(root).tool_candidates]
    assert names.count("refund") == 1


ANTHROPIC_TOOLS = '''
import anthropic

client = anthropic.Anthropic()
SYSTEM_PROMPT = "You handle refunds. Today is {TODAY}."


def lookup_customer(email):
    return {"email": email}


def issue_refund(order_id, amount):
    import requests
    return requests.post("https://pay.example.com/refunds", json={"order": order_id, "amount": amount}).json()


''' + "\n" * 120 + '''
TOOL_SCHEMAS = [
    {"name": "lookup_customer", "description": "Find a customer by email.",
     "input_schema": {"type": "object", "properties": {"email": {"type": "string"}}}},
    {"name": "issue_refund", "description": "Refund an order. Irreversible.",
     "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"},
                                                       "amount": {"type": "number"}},
                      "required": ["order_id", "amount"]}},
]


def run(messages):
    return client.messages.create(model="claude-x", max_tokens=100, system=SYSTEM_PROMPT,
                                  tools=TOOL_SCHEMAS, messages=messages)
'''

OPENAI_TOOLS = '''
from openai import OpenAI

TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city.",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
NOT_A_TOOL = {"name": "config", "retries": 3}
'''



def test_tool_literals_are_read_from_the_code(tmp_path):
    (tmp_path / "a.py").write_text(ANTHROPIC_TOOLS)
    (tmp_path / "b.py").write_text(OPENAI_TOOLS)
    found = {t.name: t for t in scan(tmp_path).static_tools}
    assert set(found) == {"lookup_customer", "issue_refund", "get_weather"}         # not NOT_A_TOOL
    assert found["issue_refund"].parameters_schema["required"] == ["order_id", "amount"]
    assert found["get_weather"].description == "Weather for a city."


AI_SDK_AGENT = '''import { generateText, tool } from 'ai';
import { z } from 'zod';

export const tools = {
  lookup_customer: tool({
    description: 'Find a customer by email.',
    inputSchema: z.object({ email: z.string() }),
    execute: async ({ email }) => db.find(email),
  }),
  issue_refund: tool({
    description: 'Refund an order. Irreversible.',
    inputSchema: z.object({ order_id: z.string(), amount: z.number() }),
    execute: async (args) => payments.refund(args),
  }),
};

export async function run(prompt: string) {
  return generateText({ model: 'openai/gpt-5', tools, prompt, temperature: 1 });
}
'''



def test_a_typescript_ai_sdk_agent_has_its_tools(tmp_path):
    """A Next.js agent (no Python): the AI SDK's tool() declarations, with their Zod schemas as JSON Schema,
    and its model call."""
    (tmp_path / "package.json").write_text('{"dependencies": {"ai": "^7.0.1", "next": "16.0.0"}}')
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "agent.ts").write_text(AI_SDK_AGENT)
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "index.js").write_text("tool({ description: 'no', inputSchema: z.object({}) })")
    found = scan(tmp_path)
    assert [t.name for t in found.static_tools] == ["lookup_customer", "issue_refund"]
    issue = found.static_tools[1].parameters_schema
    assert issue["required"] == ["order_id", "amount"]
    assert (found.framework, found.framework_version) == ("custom", "7.0.1")
    assert len(found.model_candidates) == 1 and found.relevant_files[0].endswith("lib/agent.ts")


def test_model_clients_are_found_with_their_provider(tmp_path):
    (tmp_path / "a.py").write_text("from openai import OpenAI\nclient = OpenAI()\n")
    (tmp_path / "b.py").write_text("import anthropic\nc = anthropic.Anthropic()\n")
    providers = {m.provider_hint for m in scan(tmp_path).model_candidates}
    assert {"openai", "anthropic"} <= providers


def test_the_scan_reports_tools_and_models_only(tmp_path):
    (tmp_path / "agent.py").write_text('SYSTEM_PROMPT = "You are helpful."\nALLOWED_TOOLS = ["x"]\n')
    found = scan(tmp_path)
    assert not hasattr(found, "prompt_candidates") and not hasattr(found, "guardrail_candidates")
    assert _scan.run_bootstrap is scan                        # the earlier name still reads


class TestFrameworkDetection:
    @pytest.mark.parametrize("packages, framework", [
        ({"langchain": "0.1", "langchain-openai": "0.1"}, "langchain"),
        ({"langgraph": "0.2.0"}, "langgraph"),
        ({"openai-agents": "1.0"}, "openai_agents"),
        ({"autogen": "0.2"}, "autogen"),
        ({"numpy": "1.26", "pandas": "2.0"}, "unknown"),
        ({"agents": "1.0"}, "unknown"),                       # a distribution called `agents` is not the SDK
    ])
    def test_detect(self, packages, framework):
        assert _detect_framework(packages)[0] == framework


class TestRequirementsParsing:
    def test_parse_requirements_txt(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("langchain==0.1.0\nopenai>=1.0\nnumpy")
        packages: dict = {}
        _parse_requirements_file(req_file, packages)
        assert {"langchain", "openai", "numpy"} <= set(packages)

    def test_parse_requirements_ignores_comments(self, tmp_path):
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("# comment\nlangchain==0.1\n# another comment\nopenai")
        packages: dict = {}
        _parse_requirements_file(req_file, packages)
        assert {"langchain", "openai"} <= set(packages) and "comment" not in packages
