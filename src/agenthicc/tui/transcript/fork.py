"""``/rewind``: a new session made from the first turns of another (TUX-09 §2.4).

Forking copies *bytes*, never edits: the new session's event log is the
original's up to the chosen turn's prompt, its conversation journal (the
model's memory) is the original's up to the record that opened that turn, and
its metadata is the original's. The original session is only ever read, so its
log is byte-for-byte what it was (a test compares them).

The prompt of the turn is not copied. It is written next to the new session's
log as a draft, which the session picks up when it starts and puts in the
composer (``Edit and resend from here``).

Files are not restored: a checkpoint feature is a separate PRD, and the dialog
says so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from agenthicc.memory.journal import journal_path_for
from agenthicc.tui.runtime import session_log

__all__ = ["Fork", "fork_session", "take_session_draft", "write_session_draft"]

_DRAFT_NAME = "draft.txt"
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Fork:
    """What :func:`fork_session` made."""

    session_id: str
    #: The prompt of the turn the fork was made at.
    draft: str
    #: User turns the new session keeps (the turn before the chosen one and earlier).
    turns: int
    #: Whether the model's memory was forked too (its journal had a matching turn).
    memory: bool


def _user_turns(path: Path) -> list[tuple[int, str]]:
    """``(offset of the line, text)`` of every user message in the log, in order."""
    turns: list[tuple[int, str]] = []
    position = 0
    with path.open("rb") as handle:
        for line in handle:
            if b'"user_message"' in line:
                try:
                    record = json.loads(line)
                except ValueError:
                    record = None
                if isinstance(record, dict) and record.get("kind") == "user_message":
                    payload = record.get("payload")
                    text = payload.get("text") if isinstance(payload, dict) else None
                    turns.append((position, text if isinstance(text, str) else ""))
            position += len(line)
    return turns


def _journal_cut(path: Path, texts: list[str], turn: int) -> int | None:
    """The byte offset of the journal record that opened user turn *turn*, or ``None``.

    Each user turn is matched, in order, with the next ``turn_started`` record
    whose message contains its text (the journal's message may carry injected
    mention context in front). A turn without a match is skipped; only the
    chosen turn has to match.
    """
    starts: list[tuple[int, str]] = []
    position = 0
    seen: set[str] = set()
    try:
        with path.open("rb") as handle:
            for line in handle:
                if b'"turn_started"' in line:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        record = None
                    if isinstance(record, dict) and record.get("kind") == "turn_started":
                        turn_id = str(record.get("turn_id", ""))
                        message = record.get("user_message")
                        if turn_id not in seen and isinstance(message, str):
                            seen.add(turn_id)
                            starts.append((position, message))
                position += len(line)
    except OSError:
        return None
    cursor = 0
    cut: int | None = None
    for number, text in enumerate(texts[:turn], start=1):
        found = next(
            (index for index in range(cursor, len(starts)) if text.strip() in starts[index][1]),
            None,
        )
        if found is None:
            if number == turn:
                return None
            continue
        if number == turn:
            cut = starts[found][0]
        cursor = found + 1
    return cut


def _copy_prefix(source: Path, target: Path, size: int) -> None:
    """Copy the first *size* bytes of *source* to a new private file *target*."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, source.open("rb") as handle:
        left = size
        while left > 0:
            chunk = handle.read(min(_CHUNK, left))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
        out.flush()
        os.fsync(out.fileno())


def fork_session(session_id: str, turn: int) -> Fork:
    """Make a new session holding the turns of *session_id* before user turn *turn*.

    Raises :class:`ValueError` for a turn the log does not have. The original
    session is not written to.
    """
    log = session_log.get_session_log_path(session_id)
    turns = _user_turns(log) if log.exists() else []
    if not 1 <= turn <= len(turns):
        raise ValueError(f"this session has no turn {turn}")
    offset, draft = turns[turn - 1]
    meta = session_log.load_session_metadata(session_id)
    cwd = meta.get("cwd")
    model = meta.get("model")
    new_id = session_log.create_session_id()
    session_log.register_session(
        new_id,
        cwd if isinstance(cwd, str) else os.getcwd(),
        model if isinstance(model, str) else "",
    )
    target_dir = session_log.get_session_log_path(new_id).parent
    target_dir.mkdir(parents=True, exist_ok=True)
    _copy_prefix(log, target_dir / log.name, offset)
    mode = session_log.load_session_mode(session_id)
    if mode is not None:
        session_log.update_session_mode(new_id, mode)
    agent = session_log.load_session_agent(session_id)
    if agent is not None:
        session_log.update_session_agent(new_id, agent)
    memory = False
    source_journal = journal_path_for(session_id)
    if source_journal.exists():
        cut = _journal_cut(source_journal, [text for _, text in turns], turn)
        if cut is not None:
            _copy_prefix(source_journal, journal_path_for(new_id), cut)
            memory = True
    write_session_draft(new_id, draft)
    return Fork(new_id, draft, turn - 1, memory)


def _draft_path(session_id: str) -> Path:
    return session_log.get_session_log_path(session_id).parent / _DRAFT_NAME


def write_session_draft(session_id: str, text: str) -> None:
    """Leave *text* for the session to put in its composer when it starts."""
    path = _draft_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def take_session_draft(session_id: str) -> str:
    """The draft left for *session_id*, once (``""`` when there is none)."""
    path = _draft_path(session_id)
    try:
        text = path.read_text(encoding="utf-8")
        path.unlink()
    except OSError:
        return ""
    return text
