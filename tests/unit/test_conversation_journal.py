"""Tests for the durable ConversationJournal (PRD-129 Phase 2)."""
from __future__ import annotations

import json

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


class TestPathHelper:
    def test_journal_path_for_structure(self) -> None:
        p = journal_path_for("sess-123")
        assert p.name == "conversation-journal.jsonl"
        assert p.parent.name == "sess-123"
        assert p.parent.parent.name == "sessions"
