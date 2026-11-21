"""TUX-09 §2.4 / acceptance criterion 7: ``/rewind`` forks the conversation, never the original."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenthicc.memory import journal as journal_module
from agenthicc.memory.journal import ConversationJournal, fold_path, journal_path_for
from agenthicc.tui.runtime import session_log
from agenthicc.tui.transcript.fork import fork_session, take_session_draft
from tests.transcript_support import scheduler_session, use_sessions_dir, write_session

pytestmark = pytest.mark.unit


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    sessions = tmp_path / "sessions"
    use_sessions_dir(monkeypatch, sessions)
    monkeypatch.setattr(journal_module, "_SESSIONS_DIR", sessions)
    yield sessions


def _original(root: Path, turns: int = 5) -> Path:
    """A session with *turns* user turns, its conversation journal and its metadata."""
    session_log.register_session("orig", str(root.parent), "anthropic/test-model")
    session_log.update_session_mode("orig", "Plan")
    events = scheduler_session(turns)
    log = write_session(root, "orig", events)
    prompts = [str(p["text"]) for kind, p in events if kind == "user_message"]
    journal = ConversationJournal(journal_path_for("orig"))
    for turn in range(1, turns + 1):
        # The journal's message may carry injected context in front of the prompt.
        journal.turn_started(f"turn-{turn}", f"[context]\n{prompts[turn - 1]}", 2 * (turn - 1))
        journal.append_message({"role": "user", "content": f"turn {turn}"})
        journal.append_message({"role": "assistant", "content": f"answer {turn}"})
        journal.turn_completed(f"turn-{turn}")
    journal.close()
    return log


def _prompts(path: Path) -> list[str]:
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return [r["payload"]["text"] for r in records if r["kind"] == "user_message"]


def test_choosing_turn_3_makes_a_session_with_turns_1_and_2_and_the_prompt_as_the_draft(
    root: Path,
) -> None:
    original = _original(root)
    before = original.read_bytes()
    journal_before = journal_path_for("orig").read_bytes()

    fork = fork_session("orig", 3)

    assert fork.session_id != "orig" and fork.turns == 2 and fork.memory is True
    copied = session_log.get_session_log_path(fork.session_id)
    assert _prompts(copied) == ["turn 1: look at the parser", "turn 2: look at the parser"]
    # The new log is the original's bytes up to turn 3's prompt, nothing re-encoded.
    assert before.startswith(copied.read_bytes())
    assert b"turn 3" not in copied.read_bytes()
    # The prompt of turn 3 is waiting for the new session's composer, once.
    assert fork.draft == "turn 3: look at the scheduler"
    assert take_session_draft(fork.session_id) == "turn 3: look at the scheduler"
    assert take_session_draft(fork.session_id) == ""

    # The original session is untouched: byte for byte (log and the model's memory).
    assert original.read_bytes() == before
    assert journal_path_for("orig").read_bytes() == journal_before


def test_the_forks_memory_holds_only_the_earlier_turns(root: Path) -> None:
    _original(root)
    fork = fork_session("orig", 3)
    messages, _summary = fold_path(journal_path_for(fork.session_id))
    assert [m["content"] for m in messages] == ["turn 1", "answer 1", "turn 2", "answer 2"]  # type: ignore[index]
    full, _ = fold_path(journal_path_for("orig"))
    assert len(full) == 10


def test_the_fork_keeps_the_directory_model_mode_and_is_indexed(root: Path) -> None:
    _original(root)
    fork = fork_session("orig", 2)
    metadata = session_log.load_session_metadata(fork.session_id)
    assert metadata["cwd"] == str(root.parent.resolve())
    assert metadata["model"] == "anthropic/test-model"
    assert session_log.load_session_mode(fork.session_id) == "Plan"
    assert fork.session_id in session_log._load_index()
    assert "orig" in session_log._load_index()  # the original stays in the index


def test_a_turn_the_journal_cannot_place_forks_the_log_and_says_the_memory_is_gone(
    root: Path,
) -> None:
    _original(root)
    journal_path_for("orig").write_text("")
    fork = fork_session("orig", 4)
    assert fork.memory is False
    assert len(_prompts(session_log.get_session_log_path(fork.session_id))) == 3
    assert not journal_path_for(fork.session_id).exists()


def test_repeated_identical_prompts_fork_at_the_right_one(root: Path) -> None:
    events = [
        ("user_message", {"text": "again"}),
        ("text", {"text": "one"}),
        ("user_message", {"text": "again"}),
        ("text", {"text": "two"}),
    ]
    write_session(root, "dup", events)
    journal = ConversationJournal(journal_path_for("dup"))
    for number in (1, 2):
        journal.turn_started(f"t{number}", "again", 0)
        journal.append_message({"role": "user", "content": f"again #{number}"})
        journal.turn_completed(f"t{number}")
    journal.close()
    fork = fork_session("dup", 2)
    messages, _ = fold_path(journal_path_for(fork.session_id))
    assert [m["content"] for m in messages] == ["again #1"]  # type: ignore[index]


def test_a_turn_the_log_does_not_have_is_refused_and_nothing_is_created(root: Path) -> None:
    _original(root, 3)
    with pytest.raises(ValueError, match="no turn 4"):
        fork_session("orig", 4)
    with pytest.raises(ValueError, match="no turn 0"):
        fork_session("orig", 0)
    assert sorted(path.name for path in root.iterdir() if path.is_dir()) == ["orig"]
