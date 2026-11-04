"""TOOLR-04: one result and error contract across every tool agenthicc can expose.

Every tool reachable at runtime is enumerated and run through lauren-ai's real
executor with the global hooks a runner installs. Workflow phase tools are
built inside workflow runners with run-specific state, so they are checked
statically from source instead.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from lauren_ai._tools import TOOL_META, ToolContext, _add_to_tool_map
from lauren_ai._tools._executor import ToolExecutor
from lauren_ai._transport import ToolCall

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.tools.approval import order_global_hooks
from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tools.errors import ToolErrorCode

pytestmark = pytest.mark.unit

_PROVIDER_SAFE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_WORKFLOWS = Path(__file__).resolve().parents[2] / "src" / "agenthicc" / "workflows"


def _browser_tools(tmp_path: Path) -> list[object]:
    from agenthicc.config import CloakBrowserSettings, PlaywrightSettings
    from agenthicc.tools.cloakbrowser.agent_tools import make_cloakbrowser_tools
    from agenthicc.tools.cloakbrowser.session import BrowserSessionManager
    from agenthicc.tools.playwright.agent_tools import make_playwright_tools

    cloak = BrowserSessionManager(CloakBrowserSettings(enabled=True), "contract", tmp_path)
    playwright = BrowserSessionManager(
        PlaywrightSettings(enabled=True), "contract", tmp_path, backend_name="Playwright"
    )
    return [*make_cloakbrowser_tools(cloak), *make_playwright_tools(playwright)]


def _subagent_tools() -> list[object]:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._transport._mock import MockTransport

    from agenthicc.subagents.communication import (
        AgentMessageBroker,
        PoolContinuationRegistry,
        make_child_communication_tools,
        make_parent_communication_tools,
    )
    from agenthicc.subagents.tool import make_spawn_subagents_tool

    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=MockTransport()), "mock-model", list(AGENT_TOOLS)
    )
    broker = AgentMessageBroker(conversation_id="c", parent_run_id="r", pool_id="p")
    return [
        spawn,
        *make_child_communication_tools(broker, "p:task-0"),
        *make_parent_communication_tools(PoolContinuationRegistry(), "c"),
    ]


def _instances() -> list[object]:
    """Class-form tools, as the session registers them: configured instances."""
    from agenthicc.skills.web_search import FetchPageTool, SearchWebTool

    return [SearchWebTool(api_key="test-key"), FetchPageTool()]


def _runtime_tools(tmp_path: Path) -> list[object]:
    return [*AGENT_TOOLS, *_browser_tools(tmp_path), *_subagent_tools(), *_instances()]


def _meta(tool: object) -> object:
    meta = getattr(tool, TOOL_META, None) or getattr(type(tool), TOOL_META, None)
    assert meta is not None, f"{tool!r} has no @tool() metadata"
    return meta


def _tool_map(tools: list[object]) -> dict[str, object]:
    tool_map: dict[str, object] = {}
    for tool in tools:
        if isinstance(tool, type) or getattr(tool, TOOL_META, None) is not None:
            _add_to_tool_map(tool_map, tool)
        else:
            _add_to_tool_map(tool_map, type(tool), instance=tool)
    return tool_map


def test_the_catalog_covers_at_least_sixty_runtime_tools(tmp_path: Path) -> None:
    names = [_meta(tool).name for tool in _runtime_tools(tmp_path)]  # type: ignore[attr-defined]

    assert len(names) >= 60
    assert len(names) == len(set(names)), "a provider tool name is registered twice"


def test_every_runtime_tool_declares_metadata_a_provider_accepts(tmp_path: Path) -> None:
    problems: list[str] = []
    for tool in _runtime_tools(tmp_path):
        meta = _meta(tool)
        name = str(meta.name)  # type: ignore[attr-defined]
        schema = meta.parameters["input_schema"]  # type: ignore[attr-defined]
        if not _PROVIDER_SAFE.match(name):
            problems.append(f"{name}: not a provider-safe name")
        if not str(meta.description).strip():  # type: ignore[attr-defined]
            problems.append(f"{name}: no description")
        if schema.get("type") != "object":
            problems.append(f"{name}: schema root is not an object")
        for parameter, spec in schema.get("properties", {}).items():
            if not spec.get("description"):
                problems.append(f"{name}.{parameter}: undocumented parameter")
        capabilities = get_tool_capabilities(tool)
        if not capabilities or ToolCapability.UNDECLARED in capabilities:
            problems.append(f"{name}: no capability metadata")

    assert problems == []


async def test_an_invalid_argument_returns_the_envelope_for_every_runtime_tool(
    tmp_path: Path,
) -> None:
    tools = _runtime_tools(tmp_path)
    executor = ToolExecutor(_tool_map(tools), global_hooks=order_global_hooks([]))  # type: ignore[arg-type]
    escaped: list[str] = []
    for tool in tools:
        name = str(_meta(tool).name)  # type: ignore[attr-defined]
        call = ToolCall(tool_use_id=f"bad-{name}", name=name, input={"__not_a_parameter__": 1})
        result = await executor.execute(
            call, ToolContext(agent_context=None, tool_use_id=call.tool_use_id, turn=0)
        )
        content = json.loads(result.content) if isinstance(result.content, str) else result.content
        if (
            result.is_error
            or not isinstance(content, dict)
            or content.get("ok") is not False
            or content.get("error_code") != ToolErrorCode.INVALID_ARGUMENT
            or not isinstance(content.get("retryable"), bool)
        ):
            escaped.append(f"{name}: {content!r}")

    assert escaped == []


def _workflow_tools() -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, set[str]]]:
    for path in sorted(_WORKFLOWS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators: set[str] = set()
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute):
                    decorators.add(target.attr)
                elif isinstance(target, ast.Name):
                    decorators.add(target.id)
            if "tool" in decorators:
                yield f"{path.relative_to(_WORKFLOWS)}:{node.lineno}", node, decorators


def test_every_workflow_phase_tool_is_documented_named_and_tagged() -> None:
    capability_decorators = {
        "tool_control",
        "tool_read",
        "tool_write",
        "tool_read_search",
        "tool_execute",
        "tool_network_read",
        "tool_network_write",
    }
    tools = list(_workflow_tools())
    problems: list[str] = []
    for where, node, decorators in tools:
        if not _PROVIDER_SAFE.match(node.name):
            problems.append(f"{where} {node.name}: not a provider-safe name")
        if not (ast.get_docstring(node) or "").strip():
            problems.append(f"{where} {node.name}: no description")
        if not decorators & capability_decorators:
            problems.append(f"{where} {node.name}: no capability decorator")

    assert len(tools) >= 100
    assert problems == []


def test_a_name_registered_twice_fails_registration_naming_both_sources() -> None:
    from lauren_ai._agents import agent, use_tools
    from lauren_ai._tools import tool

    from agenthicc.runners.tool_populator import populate_agent_tools

    def first_definition() -> object:
        @tool(name="duplicate_probe")
        async def probe() -> dict[str, object]:
            """First."""
            return {"ok": True}

        return probe

    def second_definition() -> object:
        @tool(name="duplicate_probe")
        async def probe() -> dict[str, object]:
            """Second."""
            return {"ok": True}

        return probe

    first, second = first_definition(), second_definition()

    @agent(model="mock-model")
    @use_tools(first)
    class _Agent: ...

    (failure,) = populate_agent_tools(_Agent(), [first, second])

    assert failure.tool == "duplicate_probe"
    assert "second_definition" in failure.source
    assert "first_definition" in failure.error


def test_the_session_catalog_registers_without_a_failure(tmp_path: Path) -> None:
    from lauren_ai._agents import agent, use_tools

    from agenthicc.runners.tool_populator import populate_agent_tools

    tools = _runtime_tools(tmp_path)

    @agent(model="mock-model")
    @use_tools(*AGENT_TOOLS)
    class _Agent: ...

    assert populate_agent_tools(_Agent(), tools) == ()


def test_no_tool_result_uses_the_old_recoverable_key() -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "agenthicc"
    offenders = [
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if '"recoverable": True' in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_cancellation_is_not_turned_into_a_result() -> None:
    from agenthicc.tools.errors import ToolContractHook

    async def run() -> None:
        from lauren_ai._tools import tool

        @tool()
        async def cancelled_probe() -> dict[str, object]:
            """Cancelled."""
            raise asyncio.CancelledError

        executor = ToolExecutor(_tool_map([cancelled_probe]), global_hooks=[ToolContractHook()])  # type: ignore[list-item]
        await executor.execute(
            ToolCall(tool_use_id="c", name="cancelled_probe", input={}),
            ToolContext(agent_context=None, tool_use_id="c", turn=0),
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


def test_every_literal_tool_error_code_is_in_the_vocabulary() -> None:
    """A tool cannot invent a code: ``reason`` carries the specific condition."""
    source = Path(__file__).resolve().parents[2] / "src" / "agenthicc"
    literal = re.compile(r'"error_code":\s*"([a-z_]+)"')
    vocabulary = {code.value for code in ToolErrorCode}
    offenders = sorted(
        f"{path.relative_to(source)}: {code}"
        for folder in ("tools", "skills", "workflows")
        for path in (source / folder).rglob("*.py")
        for code in literal.findall(path.read_text(encoding="utf-8"))
        if code not in vocabulary
    )

    assert offenders == []
