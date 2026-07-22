"""E2E: the Argon2 refactor scenario from the PRD (Section 17).

Three lauren-ai agents (refactor, test, docs) run as real ``AgentRunnerBase``
loops over the same agenthicc kernel. The test agent's run fails, a debugger
agent is spawned via a tool call, publishes its findings, and the workflow
completes. Everything flows through tool calls and kernel events only.

NOTE: no ``from __future__ import annotations`` — ``@tool()`` needs real
annotations.
"""

import asyncio

import pytest

from lauren_ai._agents import agent, use_tools
from lauren_ai._signals import SignalBus
from lauren_ai._tools import tool
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport
from lauren_ai.testing import _build_runner_for_agent

from agenthicc.kernel import (
    AppState,
    Event,
    EventProcessor,
    NodeStatus,
    SecurityPolicy,
    SystemSettings,
)

pytestmark = pytest.mark.e2e


def _completion(content: str) -> Completion:
    return Completion(
        id="c",
        model="mock-model",
        content=content,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


async def test_argon2_three_agents_with_debugger_recovery(tmp_path):
    state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
            max_parallel_tasks=5,
        ),
        policy=SecurityPolicy(),
    )
    kernel = EventProcessor(initial_state=state, persist=False)
    proc_task = asyncio.create_task(kernel.run())
    processor = kernel

    # ── Seed the workflow DAG ────────────────────────────────────────────
    await processor.emit(
        Event.create(
            "IntentCreated",
            {
                "intent_id": "i-argon2",
                "raw_text": "Refactor auth module to Argon2, add tests, update docs.",
            },
        )
    )
    await processor.emit(
        Event.create(
            "WorkflowCreated",
            {
                "workflow_id": "wf-argon2",
                "intent_id": "i-argon2",
            },
        )
    )
    for node_id, label, deps in [
        ("refactor", "Refactor auth module to Argon2", []),
        ("tests", "Write and run Argon2 tests", ["refactor"]),
        ("docs", "Update auth documentation", ["refactor"]),
    ]:
        await processor.emit(
            Event.create(
                "WorkflowNodeAdded",
                {
                    "workflow_id": "wf-argon2",
                    "node_id": node_id,
                    "task_id": f"t-{node_id}",
                    "label": label,
                    "dependencies": deps,
                },
            )
        )
    await processor.drain()

    # ── Communication tools shared by all agents ────────────────────────
    artifacts: dict[str, str] = {}

    @tool()
    async def node_status(node_id: str, status: str, result: str = "") -> dict:
        """Update a workflow node's status.

        Args:
            node_id: The node to update.
            status: New status: running, complete, or failed.
            result: Result summary or error description.
        """
        payload = {"workflow_id": "wf-argon2", "node_id": node_id, "status": status}
        if status == "failed":
            payload["error"] = result
        else:
            payload["result"] = result
        await processor.emit(Event.create("WorkflowNodeStatusChanged", payload))
        return {"ok": True}

    @tool()
    async def spawn_debugger(reason: str) -> dict:
        """Spawn a debugger agent to investigate a failure.

        Args:
            reason: Why the debugger is needed.
        """
        await processor.emit(
            Event.create(
                "AgentSpawnRequest",
                {
                    "agent_id": "debugger-1",
                    "agent_type": "DebuggerAgent",
                    "config": {"reason": reason},
                },
            )
        )
        return {"agent_id": "debugger-1"}

    @tool()
    async def publish_findings(key: str, content: str) -> dict:
        """Publish findings as a shared artifact.

        Args:
            key: Artifact key.
            content: Findings content.
        """
        artifacts[key] = content
        return {"artifact_key": key}

    @tool()
    async def read_findings(key: str) -> dict:
        """Read a previously published artifact.

        Args:
            key: Artifact key to read.
        """
        return {"found": key in artifacts, "content": artifacts.get(key, "")}

    # ── Agent classes ────────────────────────────────────────────────────
    @agent(model="mock-model", system="You refactor code.")
    @use_tools(node_status)
    class RefactorAgent: ...

    @agent(model="mock-model", system="You run tests and report failures.")
    @use_tools(node_status, spawn_debugger)
    class TestAgent: ...

    @agent(model="mock-model", system="You update documentation.")
    @use_tools(node_status)
    class DocsAgent: ...

    @agent(model="mock-model", system="You debug failures and publish fixes.")
    @use_tools(node_status, publish_findings)
    class DebuggerAgent: ...

    bus = SignalBus()

    def make_runner(agent_instance, scripted):
        mock = MockTransport()
        for item in scripted:
            if isinstance(item, tuple):
                mock.queue_tool_use(item[0], item[1])
            else:
                mock.queue_response(_completion(item))
        return _build_runner_for_agent(agent_instance, mock, signals=bus)

    # ── Phase 1: refactor agent completes its node ──────────────────────
    refactor = RefactorAgent()
    refactor_runner = make_runner(
        refactor,
        [
            (
                "node_status",
                {
                    "node_id": "refactor",
                    "status": "complete",
                    "result": "Argon2id with cost=12 implemented",
                },
            ),
            "Refactor complete.",
        ],
    )
    await refactor_runner.run(refactor, "Refactor the auth module to Argon2")
    await processor.drain()
    assert kernel.get_state().workflows["wf-argon2"].nodes["refactor"].status == NodeStatus.complete

    # ── Phase 2: tests + docs agents run in parallel ────────────────────
    tester = TestAgent()
    test_runner = make_runner(
        tester,
        [
            (
                "node_status",
                {"node_id": "tests", "status": "failed", "result": "3 failures in test_argon2.py"},
            ),
            ("spawn_debugger", {"reason": "test_argon2.py has 3 failures"}),
            "Tests failed; spawned a debugger.",
        ],
    )

    docs = DocsAgent()
    docs_runner = make_runner(
        docs,
        [
            (
                "node_status",
                {"node_id": "docs", "status": "complete", "result": "auth.md updated for Argon2"},
            ),
            "Docs updated.",
        ],
    )

    test_response, docs_response = await asyncio.gather(
        test_runner.run(tester, "Run the test suite"),
        docs_runner.run(docs, "Update the documentation"),
    )
    await processor.drain()

    s = kernel.get_state()
    assert s.workflows["wf-argon2"].nodes["tests"].status == NodeStatus.failed
    assert s.workflows["wf-argon2"].nodes["docs"].status == NodeStatus.complete
    assert "debugger-1" in s.agents
    assert s.agents["debugger-1"].agent_type == "DebuggerAgent"

    # ── Phase 3: debugger fixes the failure, publishes findings,
    #            and flips the tests node to complete ────────────────────
    debugger = DebuggerAgent()
    debugger_runner = make_runner(
        debugger,
        [
            (
                "publish_findings",
                {"key": "fix-report", "content": "Missing salt length arg; fixed in auth.py:42"},
            ),
            (
                "node_status",
                {"node_id": "tests", "status": "complete", "result": "All tests green after fix"},
            ),
            "Debugged and fixed.",
        ],
    )
    await debugger_runner.run(debugger, "Investigate the test failures")
    await processor.drain()

    # ── Final assertions: full workflow complete + artifact shared ──────
    s = kernel.get_state()
    wf = s.workflows["wf-argon2"]
    assert all(n.status == NodeStatus.complete for n in wf.nodes.values())
    assert wf.status == NodeStatus.complete
    assert artifacts["fix-report"].startswith("Missing salt")

    # Event log captured the entire story for deterministic replay.
    event_types = [e.event_type for e in kernel.event_log]
    assert "IntentCreated" in event_types
    assert "AgentSpawnRequest" in event_types
    assert event_types.count("WorkflowNodeStatusChanged") >= 4

    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)
