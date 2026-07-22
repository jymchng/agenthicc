"""Tests for run resumption — durable ledger + RunCoordinator (PRD-129 Phase 3)."""

from __future__ import annotations

import pytest

from lauren_ai import ToolResult

from agenthicc.memory.journal import (
    ConversationJournal,
    IncompleteTurn,
    fold_resume_state,
    journal_path_for,
)
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.durable_ledger import DurableIdempotencyLedger
from agenthicc.runners.run_coordinator import RunCoordinator

pytestmark = pytest.mark.unit


def _key(name: str, inp: dict) -> str:
    from lauren_ai._tools import canonical_tool_key

    return canonical_tool_key(name, inp)


# ── fold_resume_state — detecting the incomplete turn ────────────────────────


class TestFoldResumeState:
    def test_no_markers_is_none(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.append_message({"role": "user", "content": "hi"})
        j.close()
        assert fold_resume_state(jp) is None

    def test_started_without_completed_is_incomplete(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.turn_started("turn-1", "do the thing", base_count=3)
        j.tool_recorded(
            "turn-1",
            _key("write", {"p": "a"}),
            {"tool_use_id": "t1", "content": "ok", "is_error": False},
        )
        # crash — no turn_completed
        j.close()
        inc = fold_resume_state(jp)
        assert inc is not None
        assert inc.turn_id == "turn-1"
        assert inc.user_message == "do the thing"
        assert inc.base_count == 3
        assert inc.tool_records == [
            (_key("write", {"p": "a"}), {"tool_use_id": "t1", "content": "ok", "is_error": False})
        ]

    def test_completed_turn_is_not_resumed(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.turn_started("turn-1", "x", base_count=0)
        j.tool_recorded("turn-1", _key("write", {"p": "a"}), {"tool_use_id": "t1", "content": "ok"})
        j.turn_completed("turn-1")
        j.close()
        assert fold_resume_state(jp) is None

    def test_redriven_then_completed_is_not_resumed(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.turn_started("turn-1", "x", base_count=0)  # crashed
        j.turn_started("turn-1", "x", base_count=0)  # re-driven (same id)
        j.turn_completed("turn-1")
        j.close()
        assert fold_resume_state(jp) is None

    def test_only_the_last_incomplete_turn(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.turn_started("turn-1", "first", base_count=0)
        j.turn_completed("turn-1")
        j.turn_started("turn-2", "second", base_count=4)
        j.close()
        inc = fold_resume_state(jp)
        assert inc is not None and inc.turn_id == "turn-2"


# ── DurableIdempotencyLedger ─────────────────────────────────────────────────


class TestDurableLedger:
    def test_record_writes_a_tool_recorded_entry(self, tmp_path) -> None:
        import json

        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        led = DurableIdempotencyLedger(j, "turn-1")
        led.record("write", {"p": "a"}, ToolResult.ok("done", tool_use_id="t1"))
        j.close()
        entries = [json.loads(line) for line in jp.read_text().splitlines() if line.strip()]
        recorded = [e for e in entries if e["kind"] == "tool_recorded"]
        assert len(recorded) == 1
        assert recorded[0]["turn_id"] == "turn-1"
        assert recorded[0]["result"] == {"tool_use_id": "t1", "content": "done", "is_error": False}

    def test_seeded_ledger_replays_recorded_tool(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        # Session 1: record a tool under a started (but uncompleted) turn.
        j = ConversationJournal(jp)
        j.turn_started("turn-1", "go", base_count=0)
        led1 = DurableIdempotencyLedger(j, "turn-1")
        led1.record("write", {"p": "a"}, ToolResult.ok("wrote a", tool_use_id="OLD"))
        j.close()

        # Session 2: resume → seed a fresh ledger from the journal records.
        j2 = ConversationJournal(jp)
        inc = fold_resume_state(jp)
        assert inc is not None
        led2 = DurableIdempotencyLedger(j2, inc.turn_id, seed_records=inc.tool_records)
        # The recorded tool is committed (replayable) without a promote.
        replay = led2.lookup("write", {"p": "a"})
        assert replay is not None and replay.content == "wrote a"
        # consumed once
        assert led2.lookup("write", {"p": "a"}) is None
        j2.close()

    def test_seeded_ledger_does_not_replay_unrecorded_tool(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.turn_started("turn-1", "go", base_count=0)
        led1 = DurableIdempotencyLedger(j, "turn-1")
        led1.record("write", {"p": "a"}, ToolResult.ok("ok", tool_use_id="t1"))
        j.close()
        inc = fold_resume_state(jp)
        j2 = ConversationJournal(jp)
        led2 = DurableIdempotencyLedger(j2, inc.turn_id, seed_records=inc.tool_records)
        assert led2.lookup("read", {"p": "b"}) is None  # never ran → must run live
        j2.close()


# ── RunCoordinator + memory rollback (the full cycle) ────────────────────────


class TestRunCoordinatorCycle:
    def test_detect_and_build_plan(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.turn_started("turn-9", "finish it", base_count=2)
        led = DurableIdempotencyLedger(j, "turn-9")
        led.record("run_bash", {"cmd": "make"}, ToolResult.ok("built", tool_use_id="OLD"))
        j.close()

        j2 = ConversationJournal(jp)
        inc = RunCoordinator.detect_incomplete_turn(j2)
        assert inc is not None
        plan = RunCoordinator.build_resume_plan(j2, inc)
        assert plan.turn_id == "turn-9"
        assert plan.user_message == "finish it"
        assert plan.base_count == 2
        # the side-effecting tool is replayed, not re-run
        assert plan.ledger.lookup("run_bash", {"cmd": "make"}).content == "built"
        j2.close()

    def test_clean_session_has_no_resume(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.turn_started("turn-1", "x", base_count=0)
        j.turn_completed("turn-1")
        j.close()
        j2 = ConversationJournal(jp)
        assert RunCoordinator.detect_incomplete_turn(j2) is None
        j2.close()

    def test_memory_rollback_to_base_count(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        mem = JournaledShortTermMemory(j, max_tokens=32_000)
        from lauren_ai._transport import Completion, TokenUsage

        mem.add_user("h1")
        mem.add_assistant(
            Completion(
                id="c",
                model="m",
                content="a1",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )
        )
        base = len(mem._messages)
        mem.add_user("in-flight turn")  # the interrupted turn's user message
        assert len(mem._messages) == base + 1
        mem.rollback_to(base)
        assert len(mem._messages) == base
        # fold reflects the rollback
        from agenthicc.memory.journal import fold_path

        folded, _ = fold_path(jp)
        assert len(folded) == base
        j.close()


def test_journal_path_for_is_under_sessions() -> None:
    p = journal_path_for("abc")
    assert p.parent.parent.name == "sessions"
    assert isinstance(IncompleteTurn("t", "m", 0), IncompleteTurn)
