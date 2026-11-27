"""Phase ownership and terminal collection at the durable tool boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from lauren_ai._memory import ToolExchange
from lauren_ai._tools import ToolResult
from lauren_ai._transport import Completion, TokenUsage, ToolCall

from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.subagents.communication import (
    CommunicationError,
    PoolContinuationRegistry,
    make_parent_communication_tools,
)
from agenthicc.subagents.delivery import JournalNoticeSource
from agenthicc.subagents.phase import PhaseOwner, PhasePools, bind_phase_pools

OWNER = PhaseOwner("session", "run", "frame", "generate", "invocation")


def start(journal: ConversationJournal, owner: PhaseOwner) -> None:
    journal.subagent_pool_started(
        pool_id="pool",
        fingerprint="fp",
        parent_run_id="turn",
        tasks=[{"task_id": "first", "agent_type": "explorer"}],
        phase_owner=owner.record(),
    )
    journal.subagent_pool_backgrounded(pool_id="pool", tool_result={"status": "running"})


@pytest.mark.parametrize(
    "other",
    [
        None,
        replace(OWNER, conversation_id="other-session"),
        replace(OWNER, run_id="other-run"),
        replace(OWNER, frame_id="child-frame"),
        replace(OWNER, phase="parallel-peer"),
        replace(OWNER, invocation="phase-reentry"),
    ],
)
async def test_a4_phase_ownership_filters_notices_and_rehydrated_collection(
    tmp_path: Path,
    other: PhaseOwner | None,
) -> None:
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    try:
        start(journal, OWNER)
        journal.subagent_worker_result(
            pool_id="pool",
            fingerprint="fp",
            task_id="first",
            agent_type="explorer",
            label="explorer #1",
            ok=True,
            text="private finding",
            error="",
            duration_ms=1.0,
            tool_calls=[],
            changed_paths=[],
            status="completed",
        )
        journal.subagent_pool_finished(
            pool_id="pool",
            total=1,
            succeeded=1,
            failed=0,
            text="private finding",
        )
        registry = PoolContinuationRegistry()
        assert not JournalNoticeSource(journal, registry, other).has_pending()
        (notice,) = JournalNoticeSource(journal, registry, OWNER).claim()
        assert notice.pool_id == "pool" and "private finding" not in notice.text
        assert notice.message_id == "subagent-worker:pool:first:1"
        _, _, collect, list_pools, *_ = make_parent_communication_tools(
            registry, "session", journal
        )
        own_scope = PhasePools(OWNER, journal, frozenset(), available=True)
        other_scope = PhasePools(other, journal, frozenset(), available=True) if other else None
        with bind_phase_pools(other_scope):
            with pytest.raises(CommunicationError, match="unknown subagent pool"):
                await collect("pool")
            assert (await list_pools())["pools"] == []
        with bind_phase_pools(own_scope):
            assert (await collect("pool"))["results"] == "private finding"
            assert [row["pool_id"] for row in (await list_pools())["pools"]] == ["pool"]
    finally:
        journal.close()


@pytest.mark.parametrize(
    "evidence",
    ["terminal", "live", "bad-json", "invalid-shape", "foreign", "synthetic"],
)
def test_a3_only_matching_terminal_tool_results_commit_collection(
    tmp_path: Path,
    evidence: str,
) -> None:
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    try:
        memory = JournaledShortTermMemory(journal)
        scope = PhasePools(OWNER, journal, frozenset(), available=True)
        start(journal, OWNER)
        if evidence != "live":
            journal.subagent_pool_finished(
                pool_id="pool",
                total=1,
                succeeded=0,
                failed=1,
                text="terminal failure",
            )
        call = ToolCall(
            tool_use_id="collect", name="collect_subagent_results", input={"pool_id": "pool"}
        )
        memory.add_user("Collect results")
        memory.add_assistant(
            Completion(
                id="assistant",
                model="offline",
                content="",
                tool_calls=[call],
                stop_reason="tool_use",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        )
        with bind_phase_pools(scope):
            exchange = memory.begin_tool_exchange([call], run_id="turn")
            content = json.dumps(
                {
                    "pool_id": "other-pool" if evidence == "foreign" else "pool",
                    "status": "running" if evidence == "live" else "completed",
                }
            )
            if evidence == "bad-json":
                content = "{"
            elif evidence == "invalid-shape":
                content = "[]"
            results = (
                [] if evidence == "synthetic" else [ToolResult.ok(content, tool_use_id="collect")]
            )
            assert not journal.collected_phase_pools(OWNER.invocation)
            before = len(journal._subagent_view())
            memory.commit_tool_exchange(exchange, results)
            assert len(journal._subagent_view()) - before == (evidence == "terminal")
        assert (not scope.outstanding()) is (evidence == "terminal")
        other_call = ToolCall(tool_use_id="read", name="read_file", input={"path": "notes"})
        assert scope.receipts(ToolExchange.from_tool_calls([other_call]), []) == {}
    finally:
        journal.close()


@pytest.mark.parametrize("transition", ["enter_workflow", "request_plan_approval", "reject_review"])
async def test_a3_gate_blocks_approval_and_handoff_before_tool_runs(
    tmp_path: Path,
    transition: str,
) -> None:
    from lauren_ai._tools._hooks import ToolCallContext

    from agenthicc.subagents.phase import PhasePoolGate

    journal = ConversationJournal(tmp_path / "journal.jsonl")
    try:
        scope = PhasePools(OWNER, journal, frozenset({transition}), available=True)
        gate = PhasePoolGate(scope)
        request = ToolCallContext(
            agent_context=None, tool_use_id=transition, turn=1, tool_name=transition
        )
        assert not (await gate.before_tool_call(request))._aborted
        start(journal, OWNER)
        denial = await gate.before_tool_call(request)
        assert denial._aborted and denial._abort_result["error_code"] == "subagents_pending"
        journal.subagent_pool_finished(
            pool_id="pool",
            total=0,
            succeeded=0,
            failed=0,
            text="",
        )
        journal.tool_exchange_committed(
            "collection",
            call_count=1,
            completed_count=1,
            synthetic_count=0,
            phase_pool_receipts={"pool": OWNER.invocation},
        )
        assert not (await gate.before_tool_call(request))._aborted
        await gate.after_tool_call(
            {"status": "handoff_accepted"} if transition == "enter_workflow" else {"ok": True},
            request,
        )
        later_spawn = await gate.before_tool_call(
            ToolCallContext(
                agent_context=None, tool_use_id="spawn", turn=2, tool_name="spawn_subagents"
            )
        )
        if transition == "request_plan_approval":
            assert not later_spawn._aborted
        else:
            assert later_spawn._aborted
            assert later_spawn._abort_result["error_code"] == "phase_transition_started"
    finally:
        journal.close()
