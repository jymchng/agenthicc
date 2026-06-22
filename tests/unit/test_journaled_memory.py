"""Tests for JournaledShortTermMemory + the Phase 1/2 retry interaction (PRD-129)."""
from __future__ import annotations

import pytest

from lauren_ai import IdempotencyLedger, ToolResult
from lauren_ai._transport import Completion, TokenUsage, ToolCall

from agenthicc.memory.journal import ConversationJournal, fold_path
from agenthicc.memory.journaled import JournaledShortTermMemory

pytestmark = pytest.mark.unit


def _completion(text: str = "", tool_calls=None) -> Completion:
    return Completion(
        id="c",
        model="m",
        content=text,
        tool_calls=tool_calls or [],
        stop_reason="tool_use" if tool_calls else "end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _make(tmp_path) -> tuple[ConversationJournal, JournaledShortTermMemory]:
    j = ConversationJournal(tmp_path / "j.jsonl")
    return j, JournaledShortTermMemory(j, max_tokens=32_000)


class TestJournalsEveryTransition:
    def test_fold_equals_live_messages(self, tmp_path) -> None:
        j, mem = _make(tmp_path)
        mem.add_user("hello")
        mem.add_assistant(_completion("hi"))
        live = list(mem._messages)
        folded, _ = fold_path(j.path)
        assert folded == live
        j.close()

    def test_tool_results_are_journaled(self, tmp_path) -> None:
        j, mem = _make(tmp_path)
        mem.add_user("do it")
        mem.add_assistant(_completion(tool_calls=[ToolCall(tool_use_id="t1", name="f", input={})]))
        mem.add_tool_results([ToolResult.ok("done", tool_use_id="t1")])
        folded, _ = fold_path(j.path)
        assert folded == list(mem._messages)
        # last folded message is the consolidated tool_result user message
        assert folded[-1]["role"] == "user"
        j.close()


class TestResume:
    def test_construction_folds_existing_journal(self, tmp_path) -> None:
        j, mem = _make(tmp_path)
        mem.add_user("first")
        mem.add_assistant(_completion("answer"))
        j.close()

        # New process: re-open the same journal path → history restored.
        j2 = ConversationJournal(tmp_path / "j.jsonl")
        mem2 = JournaledShortTermMemory(j2, max_tokens=32_000)
        assert [m["role"] for m in mem2._messages] == ["user", "assistant"]
        assert mem2._messages[0]["content"] == "first"
        j2.close()

    def test_resume_does_not_duplicate_on_fold(self, tmp_path) -> None:
        j, mem = _make(tmp_path)
        mem.add_user("x")
        j.close()
        j2 = ConversationJournal(tmp_path / "j.jsonl")
        mem2 = JournaledShortTermMemory(j2, max_tokens=32_000)
        assert len(mem2._messages) == 1  # folded one message into memory
        # Folding into memory must NOT re-append to the journal.
        folded, _ = fold_path(j2.path)
        assert len(folded) == 1
        j2.close()


class TestRollbackAndCompaction:
    def test_restore_journals_a_reset(self, tmp_path) -> None:
        j, mem = _make(tmp_path)
        mem.add_user("u1")
        pre = mem.snapshot()
        mem.add_assistant(_completion("partial"))
        mem.restore(pre)  # retry rollback
        folded, _ = fold_path(j.path)
        assert [m["role"] for m in folded] == ["user"]
        assert folded == list(mem._messages)
        j.close()

    def test_journal_reset_keeps_compaction_in_sync(self, tmp_path) -> None:
        j, mem = _make(tmp_path)
        mem.add_user("u1")
        mem.add_assistant(_completion("a1"))
        # Simulate compactor replacing _messages in place, then notifying.
        mem._messages = [{"role": "user", "content": "[COMPACT SUMMARY]\n…"}]
        mem.journal_reset()
        folded, _ = fold_path(j.path)
        assert folded == [{"role": "user", "content": "[COMPACT SUMMARY]\n…"}]
        j.close()


class TestRetryIdempotencyWithJournal:
    """The Phase 1 ledger + Phase 2 journal cooperating across a turn retry."""

    def test_rollback_replays_tool_without_reexec_and_journal_is_correct(self, tmp_path) -> None:
        j, mem = _make(tmp_path)
        ledger = IdempotencyLedger()
        executions: list[str] = []

        def run_tool(name: str, args: dict[str, object]) -> ToolResult:
            """Execute through the ledger — replay on hit, run + record on miss."""
            hit = ledger.lookup(name, args)
            if hit is not None:
                return hit
            executions.append(name)  # the actual side effect
            res = ToolResult.ok(f"ran {name}", tool_use_id="t1")
            ledger.record(name, args, res)
            return res

        # Pre-turn baseline.
        mem.add_user("earlier turn")
        mem.add_assistant(_completion("ok"))

        # ── Attempt 1 (will be rolled back) ──────────────────────────────────
        pre_turn = mem.snapshot()
        mem.add_user("write the file")
        tc = ToolCall(tool_use_id="t1", name="write_file", input={"path": "f"})
        mem.add_assistant(_completion(tool_calls=[tc]))
        res1 = run_tool("write_file", {"path": "f"})
        mem.add_tool_results([res1])
        # …then a transient failure rolls the whole turn back: memory restores to
        # the pre-turn snapshot AND the ledger promotes so the retry can replay.
        mem.restore(pre_turn)
        ledger.promote()

        # ── Attempt 2 (the retry) ────────────────────────────────────────────
        mem.add_user("write the file")
        mem.add_assistant(_completion(tool_calls=[tc]))
        res2 = run_tool("write_file", {"path": "f"})  # ledger HIT → no re-exec
        mem.add_tool_results([res2])
        mem.add_assistant(_completion("done"))

        # The side-effecting tool ran exactly once.
        assert executions == ["write_file"], "tool must not re-execute on retry"
        # The durable journal folds to the live, correct final history.
        folded, _ = fold_path(j.path)
        assert folded == list(mem._messages)
        roles = [m["role"] for m in folded]
        # earlier user/assistant, then retry: user, assistant(tool), tool_result(user), assistant
        assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
        j.close()
