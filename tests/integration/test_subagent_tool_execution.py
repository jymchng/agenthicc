"""Mock-provider integration tests for real subagent tool execution."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor
from agenthicc.memory.journal import ConversationJournal
from agenthicc.runners.agent_turn_context import bind_background_task_owner
from agenthicc.subagents.pool import active_pool
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
from agenthicc.supervised_tasks import SupervisedTasks
from agenthicc.tools.approval import ApprovalResponse
from agenthicc.tools.capabilities import tool_write
from agenthicc.tools.fs.agent_tools import write_file
from agenthicc.tui.conversation_store import AppState, ConversationStore
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.integration


def _registry(tool_name: str) -> SubagentTypeRegistry:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="researcher",
            allowed_tools=frozenset({tool_name}),
            max_turns=3,
            system_prompt="Use the research tool and report its result.",
            max_turn_time_s=10.0,
        )
    )
    return registry


def _implementer_registry(*tool_names: str) -> SubagentTypeRegistry:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="implementer",
            allowed_tools=frozenset(tool_names),
            max_turns=3,
            system_prompt="Make the requested change with the available tools.",
            max_turn_time_s=10.0,
        )
    )
    return registry


def _final(text: str, identifier: str) -> Completion:
    return Completion(
        id=identifier,
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=3, output_tokens=5),
    )


def _tool_names(call: object) -> list[str]:
    tools = getattr(call, "tools", None) or []
    return [
        str(schema.get("name", "") if isinstance(schema, dict) else getattr(schema, "name", ""))
        for schema in tools
    ]


def _message_content(message: object) -> object:
    return message.get("content") if isinstance(message, dict) else getattr(message, "content", "")


def _contract_registry() -> SubagentTypeRegistry:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="reporter",
            allowed_tools=frozenset(),
            max_turns=3,
            system_prompt="Return the requested report.",
            max_turn_time_s=10.0,
            output_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "count": {"type": "integer", "minimum": 0},
                },
                "required": ["summary", "count"],
                "additionalProperties": False,
            },
        )
    )
    return registry


def _tool_schema(call: object, name: str) -> dict[str, object] | None:
    for schema in getattr(call, "tools", None) or []:
        if isinstance(schema, dict) and schema.get("name") == name:
            input_schema = schema.get("input_schema")
            return input_schema if isinstance(input_schema, dict) else None
        if getattr(schema, "name", None) == name:
            input_schema = getattr(schema, "input_schema", None)
            return input_schema if isinstance(input_schema, dict) else None
    return None


async def test_structured_result_submission_uses_contract_schema_and_returns_value(
    tmp_path: Path,
) -> None:
    transport = MockTransport()
    transport.queue_tool_use(
        "submit_subagent_result",
        {"summary": "two files reviewed", "count": 2},
        tool_use_id="result-1",
    )
    transport.queue_response(_final("Submitted.", "final-result"))
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    registry = _contract_registry()
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [],
        registry=registry,
        conversation_journal=journal,
    )

    try:
        result = await spawn(
            tasks=[{"type": "reporter", "task": "Count reviewed files."}],
            timeout_s=30,
        )
        worker = journal.fold_subagent_worker_results()[0]
    finally:
        journal.close()

    assert result["ok"] is True
    assert result["values"] == [
        {
            "task_id": "task-0",
            "agent_type": "reporter",
            "label": "reporter #1",
            "value": {"count": 2, "summary": "two files reviewed"},
        }
    ]
    assert '"count": 2' in str(result["results"])
    assert "submit_subagent_result" in _tool_names(transport.calls[0])
    spec = registry.get("reporter")
    assert spec is not None
    assert _tool_schema(transport.calls[0], "submit_subagent_result") == spec.output_schema
    assert worker["value"] == {"count": 2, "summary": "two files reviewed"}


async def test_structured_result_is_redacted_before_delivery_and_persistence(
    tmp_path: Path,
) -> None:
    secret = "sk-ant-structured-secret"
    transport = MockTransport()
    transport.queue_tool_use(
        "submit_subagent_result",
        {"summary": f"api_key={secret}", "count": 1},
        tool_use_id="redacted-result",
    )
    transport.queue_response(_final("Submitted.", "final-redacted-result"))
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        [],
        registry=_contract_registry(),
        conversation_journal=journal,
    )

    result = await spawn(
        tasks=[{"type": "reporter", "task": "Return a safe report."}],
        timeout_s=30,
    )

    worker = journal.fold_subagent_worker_results()[0]
    pool_result = journal.fold_subagent_pool_results()[0]
    assert result["ok"] is True
    assert secret not in str(result["values"])
    assert "<redacted>" in str(result["values"])
    assert secret not in str(worker["value"])
    assert secret not in str(pool_result["values"])
    assert secret not in str(result["results"])
    journal.close()


async def test_structured_result_lifecycle_resume_preserves_validated_values(
    tmp_path: Path,
) -> None:
    transport = MockTransport()
    transport.queue_tool_use(
        "submit_subagent_result",
        {"summary": "durable report", "count": 3},
        tool_use_id="lifecycle-result",
    )
    transport.queue_response(_final("Submitted.", "final-lifecycle-result"))
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        [],
        registry=_contract_registry(),
        parent_run_id="parent-run-structured",
        conversation_journal=journal,
    )
    tasks = [{"type": "reporter", "task": "Count reviewed files."}]

    first = await spawn(tasks=tasks, timeout_s=30)
    resumed = await spawn(tasks=tasks, timeout_s=30)

    assert first["ok"] is True
    assert resumed["ok"] is True
    assert resumed["status"] == "completed"
    assert resumed["values"] == first["values"]
    assert len(transport.calls) == 2
    journal.close()


async def test_structured_result_validation_failure_is_typed_and_counts_as_failure(
    tmp_path: Path,
) -> None:
    transport = MockTransport()
    transport.queue_tool_use(
        "submit_subagent_result",
        {"summary": "invalid count", "count": "two"},
        tool_use_id="invalid-result",
    )
    transport.queue_response(_final("The result was submitted.", "final-invalid-result"))
    runner = AgentRunnerBase(transport=transport)
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [],
        registry=_contract_registry(),
        conversation_journal=journal,
    )

    result = await spawn(
        tasks=[{"type": "reporter", "task": "Count reviewed files."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    worker = journal.fold_subagent_worker_results()[0]
    assert worker["error_code"] == "result_validation_error"
    assert "count" in str(worker["error"])
    journal.close()


async def test_contract_worker_without_submission_fails(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("A prose-only response.", "final-no-result"))
    runner = AgentRunnerBase(transport=transport)
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [],
        registry=_contract_registry(),
        conversation_journal=journal,
    )

    result = await spawn(
        tasks=[{"type": "reporter", "task": "Count reviewed files."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    worker = journal.fold_subagent_worker_results()[0]
    assert worker["error_code"] == "result_missing_error"
    journal.close()


async def test_structured_result_projector_failure_is_typed_and_counts_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.subagents import pool as pool_module

    def fail_project(_value: object) -> dict[str, object]:
        raise RuntimeError("projector unavailable")

    monkeypatch.setattr(pool_module, "project_result_value", fail_project)
    transport = MockTransport()
    transport.queue_tool_use(
        "submit_subagent_result",
        {"summary": "report", "count": 1},
        tool_use_id="projector-result",
    )
    transport.queue_response(_final("Submission attempted.", "final-projector"))
    runner = AgentRunnerBase(transport=transport)
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [],
        registry=_contract_registry(),
        conversation_journal=journal,
    )

    result = await spawn(
        tasks=[{"type": "reporter", "task": "Count reviewed files."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    worker = journal.fold_subagent_worker_results()[0]
    assert worker["error_code"] == "result_projector_error"
    journal.close()


async def test_renderer_failure_is_contained_as_a_failed_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.subagents import pool as pool_module

    def fail_render(_value: object) -> str:
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(pool_module, "render_result_value", fail_render)
    transport = MockTransport()
    transport.queue_tool_use(
        "submit_subagent_result",
        {"summary": "report", "count": 1},
        tool_use_id="renderer-result",
    )
    transport.queue_response(_final("Submission attempted.", "final-renderer"))
    runner = AgentRunnerBase(transport=transport)
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [],
        registry=_contract_registry(),
        conversation_journal=journal,
    )

    result = await spawn(
        tasks=[{"type": "reporter", "task": "Count reviewed files."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    worker = journal.fold_subagent_worker_results()[0]
    assert worker["error_code"] == "result_renderer_error"
    journal.close()


async def test_real_subagent_executes_mock_requested_tool_and_continues() -> None:
    """A provider tool call reaches the worker's actual callable executor."""
    invoked: list[str] = []

    @tool()
    async def lookup_fact(topic: str) -> str:
        """Look up a deterministic fact for a topic."""
        invoked.append(topic)
        return f"fact:{topic}"

    transport = MockTransport()
    transport.queue_tool_use("lookup_fact", {"topic": "chapter-7"}, tool_use_id="lookup-7")
    transport.queue_response(_final("The fact was incorporated.", "final-7"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [lookup_fact],
        registry=_registry("lookup_fact"),
    )

    result = await spawn(
        tasks=[{"type": "researcher", "task": "Find the chapter-7 fact."}],
        timeout_s=30,
    )

    assert result["ok"] is True
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    assert "The fact was incorporated." in result["results"]
    assert invoked == ["chapter-7"]
    assert len(transport.calls) == 2
    assert _tool_names(transport.calls[0]) == ["lookup_fact"]
    assert any(
        "fact:chapter-7" in str(_message_content(message))
        for message in transport.calls[1].messages
    )


async def test_kernel_events_are_durable_before_spawn_returns(
    fresh_appstate: KernelAppState,
) -> None:
    transport = MockTransport()
    transport.queue_response(_final("persisted worker result", "event-log-final"))
    runner = AgentRunnerBase(transport=transport)
    processor = EventProcessor(initial_state=fresh_appstate)
    processor_task = asyncio.create_task(processor.run())
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [],
        registry=_registry("read_file"),
        processor=processor,
    )
    event_types = [
        "SubagentPoolStarted",
        "SubagentStarted",
        "SubagentCompleted",
        "SubagentPoolCompleted",
    ]

    try:
        result = await spawn(tasks=[{"type": "researcher", "task": "Inspect the workspace."}])
        assert result["ok"] is True
        assert processor.applied_count == len(event_types)
    finally:
        await processor.stop()
        await processor_task

    log = Path(fresh_appstate.settings.event_log_path).read_text(encoding="utf-8")
    assert re.findall(r'"event_type":\s*"([^\"]+)"', log) == event_types


async def test_ctrl_b_backgrounds_a_pool_and_keeps_it_session_owned(
    tmp_path,
) -> None:
    """A durable receipt detaches workers, which remain owned by the session."""
    release = asyncio.Event()

    @tool()
    async def wait_for_release(topic: str) -> str:
        """Wait until the test releases the worker."""
        await release.wait()
        return f"released:{topic}"

    transport = MockTransport()
    transport.queue_tool_use(
        "wait_for_release",
        {"topic": "background"},
        tool_use_id="wait-background",
    )
    transport.queue_response(_final("worker complete", "final-background"))
    runner = AgentRunnerBase(transport=transport)
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    conv_store = ConversationStore()
    supervisor = SupervisedTasks("background pool test")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [wait_for_release],
        registry=_registry("wait_for_release"),
        conversation_id="conversation-1",
        parent_run_id="turn-1",
        conversation_journal=journal,
        conv_store=conv_store,
    )

    try:
        with bind_background_task_owner(supervisor):
            spawn_task = asyncio.create_task(
                spawn(
                    tasks=[{"type": "researcher", "task": "Wait for the test release."}],
                    timeout_s=30,
                )
            )
        for _ in range(200):
            state = conv_store.subagent_pool_state()
            if state is not None and state.active:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("subagent pool did not start")

        pool = active_pool(state.pool_id)
        assert pool is not None
        assert pool.request_background()
        receipt = await asyncio.wait_for(spawn_task, timeout=2)
        assert receipt["status"] == "backgrounded"
        replayed = await spawn(
            tasks=[{"type": "researcher", "task": "Wait for the test release."}],
            timeout_s=30,
        )
        assert replayed == receipt
        assert supervisor.pending == 1
        assert len(journal.fold_subagent_pool_lifecycle()) == 1

        release.set()
        assert await supervisor.wait(timeout=5)
        lifecycle = journal.fold_subagent_pool_lifecycle()[0]
        assert lifecycle["status"] == "completed"
        assert lifecycle["tool_result"] == receipt
        assert lifecycle["text"]
        assert supervisor.pending == 0
    finally:
        release.set()
        await supervisor.aclose(timeout=0.1)
        journal.close()


async def test_session_shutdown_cancels_a_backgrounded_pool(tmp_path) -> None:
    """Closing the owning session records cancellation rather than orphaning workers."""
    release = asyncio.Event()

    @tool()
    async def wait_for_release(topic: str) -> str:
        """Wait until the test releases the worker."""
        await release.wait()
        return topic

    transport = MockTransport()
    transport.queue_tool_use(
        "wait_for_release",
        {"topic": "background"},
        tool_use_id="wait-background-cancel",
    )
    runner = AgentRunnerBase(transport=transport)
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    conv_store = ConversationStore()
    supervisor = SupervisedTasks("background pool cancellation test")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [wait_for_release],
        registry=_registry("wait_for_release"),
        conversation_id="conversation-1",
        parent_run_id="turn-2",
        conversation_journal=journal,
        conv_store=conv_store,
    )

    try:
        with bind_background_task_owner(supervisor):
            spawn_task = asyncio.create_task(
                spawn(
                    tasks=[{"type": "researcher", "task": "Wait for shutdown."}],
                    timeout_s=30,
                )
            )
        for _ in range(200):
            state = conv_store.subagent_pool_state()
            if state is not None and state.active:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("subagent pool did not start")
        pool = active_pool(state.pool_id)
        assert pool is not None and pool.request_background()
        receipt = await asyncio.wait_for(spawn_task, timeout=2)
        assert receipt["status"] == "backgrounded"

        await supervisor.aclose(timeout=0)
        lifecycle = journal.fold_subagent_pool_lifecycle()[0]
        assert lifecycle["status"] == "cancelled"
        worker = journal.fold_subagent_worker_results()[0]
        assert worker["status"] == "cancelled"
        assert supervisor.pending == 0
    finally:
        release.set()
        await supervisor.aclose(timeout=0.1)
        journal.close()


async def test_real_pool_runs_two_tool_workers_and_filters_unallowed_tools() -> None:
    """Each real worker can execute tools while the pool preserves both results."""
    invoked: list[str] = []

    @tool()
    async def lookup_fact(topic: str) -> str:
        """Look up a deterministic fact for a topic."""
        invoked.append(topic)
        return f"fact:{topic}"

    @tool()
    async def write_forbidden(topic: str) -> str:
        """A tool that must not be exposed to this researcher type."""
        raise AssertionError(f"unallowed tool was executed: {topic}")

    transport = MockTransport()
    transport.queue_tool_use("lookup_fact", {"topic": "alpha"}, tool_use_id="lookup-alpha")
    transport.queue_tool_use("lookup_fact", {"topic": "beta"}, tool_use_id="lookup-beta")
    transport.queue_response(_final("alpha complete", "final-alpha"))
    transport.queue_response(_final("beta complete", "final-beta"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [lookup_fact, write_forbidden],
        registry=_registry("lookup_fact"),
    )

    result = await spawn(
        tasks=[
            {"type": "researcher", "task": "Research alpha."},
            {"type": "researcher", "task": "Research beta."},
        ],
        max_concurrent=2,
        timeout_s=30,
    )

    assert result["ok"] is True
    assert result["total"] == 2
    assert result["succeeded"] == 2
    assert result["failed"] == 0
    assert "alpha complete" in result["results"]
    assert "beta complete" in result["results"]
    assert sorted(invoked) == ["alpha", "beta"]
    assert len(transport.calls) == 4
    assert all(_tool_names(call) == ["lookup_fact"] for call in transport.calls)


async def test_real_implementer_exposes_the_filesystem_write_tool() -> None:
    """The production implementer path exposes the real write-tool schema."""
    transport = MockTransport()
    transport.queue_response(_final("Ready to write chapters/01.md.", "final-1"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [write_file],
        registry=_implementer_registry("write_file"),
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    assert len(transport.calls) == 1
    assert _tool_names(transport.calls[0]) == ["write_file"]


async def test_implementer_prose_without_a_tool_is_reported_as_failure() -> None:
    """An implementer that only claims a change is not a successful worker."""
    transport = MockTransport()
    transport.queue_response(_final("I would expand chapter 1 next.", "final-noop"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [write_file],
        registry=_implementer_registry("write_file"),
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["succeeded"] == 0
    assert result["failed"] == 1
    assert "no mutation was recorded" in str(result["results"])


async def test_empty_final_response_gets_a_tool_execution_summary() -> None:
    """Tool work remains visible even when a provider omits final prose."""
    invoked: list[str] = []

    @tool()
    async def lookup_fact(topic: str) -> str:
        """Look up a deterministic fact for a topic."""
        invoked.append(topic)
        return f"fact:{topic}"

    transport = MockTransport()
    transport.queue_tool_use("lookup_fact", {"topic": "chapter-9"}, tool_use_id="lookup-9")
    transport.queue_response(_final("", "final-empty"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [lookup_fact],
        registry=_registry("lookup_fact"),
    )

    result = await spawn(
        tasks=[{"type": "researcher", "task": "Find the chapter-9 fact."}],
        timeout_s=30,
    )

    assert result["ok"] is True
    assert invoked == ["chapter-9"]
    assert "Executed tool call(s): lookup_fact." in str(result["results"])


async def test_empty_final_response_gets_one_finalization_turn() -> None:
    """A gateway empty-final quirk must not discard the requested artefact."""
    transport = MockTransport()
    transport.queue_tool_use(
        "lookup_fact",
        {"topic": "chapter-10"},
        tool_use_id="lookup-10",
    )
    transport.queue_response(_final("", "final-empty-10"))
    transport.queue_response(
        _final("The complete rewritten chapter-10 content.", "final-recovered-10")
    )
    runner = AgentRunnerBase(transport=transport)

    @tool()
    async def lookup_fact(topic: str) -> str:
        return f"fact:{topic}"

    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [lookup_fact],
        registry=_registry("lookup_fact"),
    )
    result = await spawn(
        tasks=[{"type": "researcher", "task": "Return the full rewritten chapter-10 content."}],
        timeout_s=30,
    )

    assert result["ok"] is True
    assert "The complete rewritten chapter-10 content." in str(result["results"])
    assert len(transport.calls) == 3


async def test_implementer_success_requires_a_successful_mutating_tool_call() -> None:
    """A successful implementer result records the mutation evidence."""
    changed: list[str] = []

    @tool_write
    @tool()
    async def record_change(path: str, content: str) -> dict[str, object]:
        """Record a deterministic file change for the integration test."""
        changed.append(f"{path}:{content}")
        return {"ok": True, "path": path}

    transport = MockTransport()
    transport.queue_tool_use(
        "record_change",
        {"path": "chapters/01.md", "content": "expanded"},
        tool_use_id="record-1",
    )
    transport.queue_response(_final("Changed chapters/01.md.", "final-change"))
    runner = AgentRunnerBase(transport=transport)
    registry = _implementer_registry("record_change")
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [record_change],
        registry=registry,
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is True
    assert changed == ["chapters/01.md:expanded"]
    assert "Changed chapters/01.md." in str(result["results"])


@pytest.mark.parametrize("parent_mode", ["Safe", "Plan"])
async def test_subagent_inherits_parent_policy(parent_mode: str) -> None:
    """Safe/Plan policy is enforced in an isolated child state."""
    approvals: list[str] = []
    changed: list[str] = []

    @tool_write
    @tool()
    async def approved_change(path: str, content: str) -> dict[str, object]:
        """Record a deterministic approved change."""
        changed.append(f"{path}:{content}")
        return {"ok": True, "path": path}

    class Approval:
        async def request_approval(self, request: object) -> ApprovalResponse:
            approvals.append(str(getattr(request, "tool_name", "")))
            return ApprovalResponse(allowed=False)

    transport = MockTransport()
    transport.queue_tool_use(
        "approved_change",
        {"path": "chapters/01.md", "content": "expanded"},
        tool_use_id="approved-1",
    )
    transport.queue_response(_final("Approved change completed.", "approved-final"))
    runner = AgentRunnerBase(transport=transport)
    parent_state = AppState.create()
    parent_state.active_mode.set(build_default_registry().get(parent_mode))
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [approved_change],
        registry=_implementer_registry("approved_change"),
        app_state=parent_state,
        approval_svc=Approval(),
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    assert changed == []
    assert approvals == (["approved_change"] if parent_mode == "Safe" else [])
    assert parent_state.active_mode().name == parent_mode


async def test_subagent_can_pause_for_parent_clarification_and_resume() -> None:
    """The child question uses continuation tools instead of re-entering the parent."""
    transport = MockTransport()
    transport.queue_tool_use(
        "ask_parent",
        {
            "question": "Which API version should I use?",
            "context": "Both v1 and v2 exist.",
        },
        tool_use_id="question-1",
    )
    transport.queue_response(_final("Waiting for the API version.", "question-final"))
    transport.queue_response(_final("I used the selected API version.", "resumed-final"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [],
        registry=_registry("not-a-parent-tool"),
        conversation_id="conversation-clarification",
        parent_run_id="run-clarification",
    )

    pending = await spawn(
        tasks=[{"type": "researcher", "task": "Choose the correct API version."}],
        timeout_s=30,
    )
    assert pending["status"] == "awaiting_clarification"
    question = pending["pending_questions"][0]
    communication_tools = spawn.__agenthicc_subagent_communication_tools__
    answer_tool = next(tool for tool in communication_tools if tool.__name__ == "answer_subagent")
    collect_tool = next(
        tool for tool in communication_tools if tool.__name__ == "collect_subagent_results"
    )

    answered = await answer_tool(
        pending["pool_id"],
        question["question_id"],
        "Use v2.",
    )
    assert answered["status"] == "answered"
    resumed = await collect_tool(pending["pool_id"])
    assert resumed["ok"] is True
    assert "selected API version" in resumed["results"]


async def test_implementer_rejects_a_mutating_tool_error_result() -> None:
    """A write tool returning ``ok=False`` is not mutation evidence."""

    @tool_write
    @tool()
    async def rejected_change(path: str, content: str) -> dict[str, object]:
        """Return a deterministic write failure for the integration test."""
        return {"ok": False, "error": "permission denied"}

    transport = MockTransport()
    transport.queue_tool_use(
        "rejected_change",
        {"path": "chapters/01.md", "content": "expanded"},
        tool_use_id="record-rejected",
    )
    transport.queue_response(_final("I could not write the chapter.", "final-rejected"))
    runner = AgentRunnerBase(transport=transport)
    spawn = make_spawn_subagents_tool(
        runner,
        "mock-model",
        [rejected_change],
        registry=_implementer_registry("rejected_change"),
    )

    result = await spawn(
        tasks=[{"type": "implementer", "task": "Expand chapter 1."}],
        timeout_s=30,
    )

    assert result["ok"] is False
    assert result["failed"] == 1
    assert "no mutation was recorded" in str(result["results"])


async def test_nested_depth_tier_clamps_concurrency_and_reports_inherited_limit() -> None:
    from agenthicc.config import SubagentDepthLimit, SubagentSettings

    transport = MockTransport()
    transport.queue_response(_final("The API is compatible.", "nested-limit-final"))
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        [],
        registry=_registry("not-a-parent-tool"),
        subagent_settings=SubagentSettings(
            max_depth=3,
            max_concurrent=16,
            depth_limits={2: SubagentDepthLimit(max_concurrent=2)},
        ),
        parent_depth=2,
        parent_pool_id="parent-pool",
    )

    result = await spawn(
        tasks=[{"type": "researcher", "task": "Check API compatibility."}],
        max_concurrent=99,
        timeout_s=30,
    )

    assert result["ok"] is True
    assert result["depth"] == 3
    assert result["parent_pool_id"] == "parent-pool"
    assert result["max_concurrent_used"] == 2
    assert result["max_concurrent_limit"] == 2
    assert result["max_concurrent_limit_source"] == "subagents.depth_limits.2.max_concurrent"


async def test_synthetic_delegating_type_cannot_spawn_past_max_depth() -> None:
    from agenthicc.config import SubagentSettings

    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="delegator",
            allowed_tools=frozenset({"spawn_subagents"}),
            max_turns=1,
            system_prompt="Delegate work.",
        )
    )
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=MockTransport()),
        "mock-model",
        [],
        registry=registry,
        subagent_settings=SubagentSettings(max_depth=1),
        parent_depth=1,
        parent_pool_id="root-pool",
    )

    result = await spawn(tasks=[{"type": "delegator", "task": "Repeat this task."}])

    assert result["ok"] is False
    assert result["status"] == "refused"
    assert result["depth"] == 2
    assert result["parent_pool_id"] == "root-pool"
    assert "current depth 2, limit 1" in result["error"]
    assert "subagents.max_depth" in result["error"]
