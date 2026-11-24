"""SUBAGENT-18 parent and worker tool inheritance at provider boundaries."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from collections.abc import Awaitable
from dataclasses import replace
from typing import cast
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport._mock import MockTransport

from agenthicc.agent_tools import AGENT_TOOLS, OUTLOOK_AGENT_TOOLS
from agenthicc.agents.definitions import AgentSpec
from agenthicc.agents.registry import AgentsRegistry
from agenthicc.config import AgenthiccConfig
from agenthicc.kernel.processor import EventProcessor
from agenthicc.memory.journal import ConversationJournal
from agenthicc.runners import tool_populator
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.runners.workflow_execution_context import bind_workflow_execution
from agenthicc.subagents.tool import SubagentToolSelection, make_spawn_subagents_tool
from agenthicc.subagents.types import (
    DEFAULT_REGISTRY,
    SubagentPermissionConstraints,
    _BUILTIN_REGISTRY,
)
from agenthicc.tools.base import tool_name
from agenthicc.tools.surface import ToolSurface
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceScope
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry
from tests.e2e.self_extension.harness import reply
from tests.subagent_sessions import attach, tui

pytestmark = pytest.mark.integration

_BUILTINS = frozenset(map(tool_name, AGENT_TOOLS))
_OUTLOOK = frozenset(map(tool_name, OUTLOOK_AGENT_TOOLS))


def _spawn(transport: MockTransport, role: str, *, task: str = "Check the fixture.") -> None:
    transport.queue_tool_use(
        "spawn_subagents",
        {"tasks": [{"type": role, "task": task, "context": "Fixture slug parity-18."}]},
    )


def _schemas(tools: list[object] | None) -> dict[str, dict[str, object]]:
    numbered = [item for item in tools or () if isinstance(item, dict)]
    schemas = {str(item["name"]): item for item in numbered}
    assert len(schemas) == len(numbered)
    return schemas


@pytest.mark.parametrize("outlook", [True, False], ids=["56-with-outlook", "47-without-outlook"])
@pytest.mark.parametrize("role", _BUILTIN_REGISTRY.names())
async def test_a1_a2_builtin_worker_gets_each_parent_schema_and_can_write(
    role: str, outlook: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if outlook:
        monkeypatch.setenv("MSGRAPH_TOKEN", "fixture-token")
    else:
        monkeypatch.delenv("MSGRAPH_TOKEN", raising=False)
        monkeypatch.setattr("sys.platform", "linux")  # exclude the Outlook COM backend
    tool_bindings: dict[str, dict[str, object]] = {}
    populate = tool_populator.populate_agent_tools

    def record(
        agent: object, tools: Sequence[object]
    ) -> tuple[tool_populator.ToolRegistrationFailure, ...]:
        tool_bindings[type(agent).__name__] = {tool_name(item): item for item in tools}
        return populate(agent, tools)

    monkeypatch.setattr(tool_populator, "populate_agent_tools", record)
    transport = MockTransport()
    _spawn(transport, role, task="Write the fixture result to findings.txt.")
    transport.queue_tool_use("write_file", {"path": "findings.txt", "content": "parity verified"})
    transport.queue_response(reply("Findings written to findings.txt."))
    transport.queue_response(reply("The worker finished."))

    async with tui(
        tmp_path, monkeypatch, config_text='[subagents]\nspawn_mode = "foreground"\n'
    ) as session:
        attach(session, transport)
        await session.agent_task_body("Delegate a fixture task.", turn_id="parity-check")
        assert Path("findings.txt").read_text() == "parity verified"

    assert len(transport.calls) == 4
    parent, worker, _, follow_up = transport.calls
    parent_schemas = _schemas(parent.tools)
    worker_schemas = _schemas(worker.tools)
    expected = _BUILTINS if outlook else _BUILTINS - _OUTLOOK
    assert len(expected) == (56 if outlook else 47)
    assert expected == parent_schemas.keys() & _BUILTINS
    assert expected == worker_schemas.keys() & _BUILTINS
    assert {name: parent_schemas[name] for name in expected} == {
        name: worker_schemas[name] for name in expected
    }
    assert "spawn_subagents" not in worker_schemas
    parent_bindings = tool_bindings["_AgenthiccAgent"]
    worker_bindings = tool_bindings["_SubAgent"]
    assert all(parent_bindings[name] is worker_bindings[name] for name in expected)
    spec = DEFAULT_REGISTRY.get(role)
    assert spec is not None and spec.system_prompt in str(worker.system)
    assert "Fixture slug parity-18." in str(worker.system)
    assert "Findings written to findings.txt." in str(follow_up.messages)


async def test_a2_worker_inherits_the_restricted_workflow_turn(
    running_processor: EventProcessor, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    transport = MockTransport()
    _spawn(transport, "researcher")
    transport.queue_response(reply("Fixture findings."))
    transport.queue_response(reply("Phase complete."))
    allowed = frozenset({"read_file", "grep_files", "spawn_subagents"})

    with bind_workflow_execution("parity-review-phase"):
        await _run_agent_turn(
            "Review the fixture for this phase.",
            runner=AgentRunnerBase(transport=transport),
            processor=running_processor,
            session_memory=ShortTermMemory(),
            exec_cfg=AgenthiccConfig().execution,
            allowed_tool_names=allowed,
        )

    parent, worker, _ = transport.calls
    assert _schemas(parent.tools).keys() == allowed
    assert _schemas(worker.tools).keys() & _BUILTINS == {"read_file", "grep_files"}
    assert _schemas(parent.tools)["read_file"] == _schemas(worker.tools)["read_file"]


@pytest.mark.parametrize("case", ["inherit", "restricted", "empty"])
async def test_a3_custom_definition_permissions_reach_worker_dispatch(
    case: str,
    running_processor: EventProcessor,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scope").mkdir()
    (tmp_path / "scope" / "allowed.txt").write_text("allowed content", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside content", encoding="utf-8")
    transport = MockTransport()
    _spawn(transport, "audit-worker")
    if case == "restricted":
        transport.queue_tool_use("read_file", {"path": "../outside.txt"})
        transport.queue_tool_use("read_file", {"path": "allowed.txt"})
    transport.queue_response(reply("Audit finished."))
    transport.queue_response(reply("Parent finished."))
    registry = AgentsRegistry()
    registry.register(
        AgentSpec(
            name="audit-worker",
            description="Audit fixture files.",
            mode="subagent",
            source="project",
            tools=(
                ("fs.*", "git_status") if case == "restricted" else () if case == "empty" else None
            ),
            capabilities=("read", "search") if case == "restricted" else None,
            permissions=(
                SubagentPermissionConstraints(
                    deny_tools=frozenset({"grep_*"}), workspace_paths=("scope",)
                )
                if case == "restricted"
                else None
            ),
        )
    )
    state = AppState.create()
    state.active_mode.set(build_default_registry().get("Yolo"))
    await _run_agent_turn(
        "Delegate the audit.",
        runner=AgentRunnerBase(transport=transport),
        processor=running_processor,
        session_memory=ShortTermMemory(),
        exec_cfg=AgenthiccConfig().execution,
        agent_config=AgenthiccConfig(),
        app_state=state,
        workspace_access=WorkspaceAccessPolicy(
            WorkspaceScope.create(tmp_path), mode_provider=state.active_mode
        ),
        agents_registry=registry,
    )
    parent, worker, *_ = transport.calls
    inherited = _schemas(worker.tools).keys() & _BUILTINS
    if case == "inherit":
        assert inherited == _schemas(parent.tools).keys() & _BUILTINS
    elif case == "empty":
        assert not inherited
    else:
        assert "read_file" in inherited
        assert "list_directory" in inherited
        assert not inherited & {"write_file", "git_status", "grep_files"}
        worker_messages = str(transport.calls[-2].messages)
        assert "outside content" not in worker_messages
        assert "outside_workspace" in worker_messages
        assert "allowed content" in worker_messages


async def test_a4_researcher_reads_checks_and_writes_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = MockTransport()
    _spawn(transport, "researcher", task="Read the corpus, check it, and write notes.")
    transport.queue_tool_use("read_file", {"path": "corpus.txt"})
    transport.queue_tool_use("run_python_expr", {"expression": "int('21') * 2"})
    transport.queue_tool_use("git_status", {})
    transport.queue_tool_use("inspect_agenthicc_source", {"target": "agenthicc.agent_tools"})
    transport.queue_tool_use(
        "write_file", {"path": "notes.md", "content": "Verified result is 42."}
    )
    transport.queue_response(reply("Recorded the checked result in notes.md."))
    transport.queue_response(reply("The notes are ready."))

    async with tui(
        tmp_path, monkeypatch, config_text='[subagents]\nspawn_mode = "foreground"\n'
    ) as session:
        Path("corpus.txt").write_text("Corpus sample value is 21.", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], check=True, timeout=10)
        attach(session, transport)
        await session.agent_task_body("Delegate the research task.", turn_id="research-parity")
        assert Path("notes.md").read_text() == "Verified result is 42."

    final_worker_messages = str(transport.calls[-2].messages)
    assert "Corpus sample value is 21." in final_worker_messages
    assert "42" in final_worker_messages
    assert "corpus.txt" in str(transport.calls[4].messages[-1])
    assert "AGENT_TOOLS" in str(transport.calls[5].messages[-1])


async def test_a7_result_cache_reuses_only_the_same_worker_surface(tmp_path: Path) -> None:
    transport = MockTransport()
    registry = DEFAULT_REGISTRY.copy()
    researcher = registry.get("researcher")
    assert researcher is not None
    registry.register(replace(researcher, allowed_tools=frozenset({"read_file"})))
    read, write, grep = (
        next(item for item in AGENT_TOOLS if tool_name(item) == name)
        for name in ("read_file", "write_file", "grep_files")
    )
    surface = ToolSurface(0, (read, write))
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        lambda: SubagentToolSelection(surface),
        registry=registry,
        conversation_journal=journal,
        parent_run_id="parity-cache",
    )

    async def invoke() -> dict[str, object]:
        return await cast(
            "Awaitable[dict[str, object]]",
            spawn(tasks=[{"type": "researcher", "task": "Report the fixture."}]),
        )

    async def new_result(text: str, expected_names: set[str]) -> dict[str, object]:
        transport.queue_response(reply(text))
        result = await invoke()
        assert result["ok"] is True and text in str(result["results"])
        assert _schemas(transport.calls[-1].tools).keys() & _BUILTINS == expected_names
        return result

    try:
        legacy = await new_result("Prior tool list", {"read_file"})
        assert (await invoke())["pool_id"] == legacy["pool_id"]
        assert len(transport.calls) == 1

        registry.register(researcher)
        expanded = await new_result("Expanded tool list", {"read_file", "write_file"})
        assert expanded["pool_id"] != legacy["pool_id"]
        surface = ToolSurface(0, (read, write, grep))
        await new_result("Added tool", {"read_file", "write_file", "grep_files"})

        registry.register(
            replace(
                researcher,
                permissions=SubagentPermissionConstraints(deny_tools=frozenset({"write_file"})),
            )
        )
        await new_result("New permissions", {"read_file", "grep_files"})
        surface = ToolSurface(1, surface.tools)
        updated = await new_result("New generation", {"read_file", "grep_files"})
        assert (await invoke())["pool_id"] == updated["pool_id"]
        assert len(transport.calls) == 5
    finally:
        journal.close()
