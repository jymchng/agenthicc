"""Tests for the durable ConversationJournal (PRD-129 Phase 2)."""

from __future__ import annotations

import json
import os

import pytest

from agenthicc.memory.journal import ConversationJournal, fold_path, journal_path_for

pytestmark = pytest.mark.unit


def _msg(role: str, text: str) -> dict[str, object]:
    return {"role": role, "content": text}


class TestAppendAndFold:
    def test_append_then_fold(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.append_message(_msg("user", "hi"))
        j.append_message(_msg("assistant", "hello"))
        j.close()

        messages, summary = fold_path(jp)
        assert messages == [_msg("user", "hi"), _msg("assistant", "hello")]
        assert summary is None

    def test_fold_missing_file_is_empty(self, tmp_path) -> None:
        assert fold_path(tmp_path / "nope.jsonl") == ([], None)

    def test_reset_replaces_history(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.append_message(_msg("user", "a"))
        j.append_message(_msg("assistant", "b"))
        # Rollback to just the first message + a summary.
        j.reset([_msg("user", "a")], "the summary")
        j.append_message(_msg("assistant", "c"))
        j.close()

        messages, summary = fold_path(jp)
        assert messages == [_msg("user", "a"), _msg("assistant", "c")]
        assert summary == "the summary"

    def test_seq_is_monotonic_and_persisted(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.append_message(_msg("user", "1"))
        j.append_message(_msg("user", "2"))
        j.close()
        seqs = [json.loads(line)["seq"] for line in jp.read_text().splitlines() if line.strip()]
        assert seqs == [0, 1]

        # Re-open (resume) → seq continues from where it left off.
        j2 = ConversationJournal(jp)
        j2.append_message(_msg("user", "3"))
        j2.close()
        seqs2 = [json.loads(line)["seq"] for line in jp.read_text().splitlines() if line.strip()]
        assert seqs2 == [0, 1, 2]

    def test_transactional_replace_commits_full_projection(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        journal = ConversationJournal(jp)
        journal.append_message(_msg("user", "old"))
        candidate = [_msg("user", "[COMPACT SUMMARY]"), _msg("assistant", "ok")]

        journal.replace_state(candidate, "rolling summary")
        journal.close()

        assert fold_path(jp) == (candidate, "rolling summary")

    def test_uncommitted_replace_rolls_back_on_fold_and_next_append(
        self, tmp_path, monkeypatch
    ) -> None:
        jp = tmp_path / "j.jsonl"
        journal = ConversationJournal(jp)
        journal.append_message(_msg("user", "original"))
        original_write = ConversationJournal._write

        def fail_commit(journal_arg, entry):  # noqa: ANN001, ARG001
            if entry.get("kind") == "state_replace_commit":
                raise OSError("simulated commit failure")
            original_write(journal_arg, entry)

        monkeypatch.setattr(ConversationJournal, "_write", fail_commit)
        with pytest.raises(OSError):
            journal.replace_state([_msg("user", "candidate")], None)

        # Recovery folds the prepare+candidate pair as the previous state.
        assert fold_path(jp) == ([_msg("user", "original")], None)
        monkeypatch.setattr(ConversationJournal, "_write", original_write)
        journal.append_message(_msg("assistant", "after recovery"))
        journal.close()
        assert fold_path(jp) == (
            [_msg("user", "original"), _msg("assistant", "after recovery")],
            None,
        )


class TestDurabilityAndCrash:
    def test_each_append_is_flushed_to_disk(self, tmp_path) -> None:
        # No close() — a crash would leave the handle open, but fsync means the
        # bytes are already durable.  Fold a fresh read.
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.append_message(_msg("user", "durable"))
        messages, _ = fold_path(jp)  # read without closing
        assert messages == [_msg("user", "durable")]
        j.close()

    def test_corrupt_trailing_line_is_skipped(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        j = ConversationJournal(jp)
        j.append_message(_msg("user", "good"))
        j.close()
        # Simulate a crash mid-write: append a half-written JSON line.
        with jp.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 1, "kind": "append", "message": {"role": "ass')
        messages, _ = fold_path(jp)
        assert messages == [_msg("user", "good")], "partial trailing line must be ignored"

    def test_failed_fsync_truncates_uncommitted_record(self, tmp_path, monkeypatch) -> None:
        jp = tmp_path / "j.jsonl"
        journal = ConversationJournal(jp)
        real_fsync = os.fsync
        calls = 0

        def fail_first_fsync(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated fsync failure")
            real_fsync(descriptor)

        monkeypatch.setattr("agenthicc.memory.journal.os.fsync", fail_first_fsync)
        with pytest.raises(OSError):
            journal.append_message(_msg("user", "must not become visible"))
        assert fold_path(jp) == ([], None)

        journal.append_message(_msg("user", "subsequent durable message"))
        journal.close()
        assert fold_path(jp) == ([_msg("user", "subsequent durable message")], None)

    def test_subagent_results_are_durable_and_not_provider_messages(self, tmp_path) -> None:
        jp = tmp_path / "j.jsonl"
        full_output = "chapter content\n" + ("x" * 5_000)
        journal = ConversationJournal(jp)
        journal.subagent_worker_result(
            pool_id="pool-1",
            fingerprint="fp-1",
            task_id="task-1",
            agent_type="documenter",
            label="documenter #1",
            ok=True,
            text=full_output,
            error="",
            duration_ms=12.5,
            tool_calls=["read_file"],
            changed_paths=[],
        )
        journal.subagent_pool_result(
            pool_id="pool-1",
            fingerprint="fp-1",
            total=1,
            succeeded=1,
            failed=0,
            text=full_output,
        )
        assert fold_path(jp) == ([], None)
        assert journal.fold_subagent_worker_results()[0]["text"] == full_output
        assert journal.fold_subagent_pool_results()[0]["text"] == full_output
        journal.close()

        reopened = ConversationJournal(jp)
        assert reopened.fold_subagent_pool_results()[0]["text"] == full_output
        reopened.close()


class TestPathHelper:
    def test_journal_path_for_structure(self) -> None:
        p = journal_path_for("sess-123")
        assert p.name == "conversation-journal.jsonl"
        assert p.parent.name == "sess-123"
        assert p.parent.parent.name == "sessions"
