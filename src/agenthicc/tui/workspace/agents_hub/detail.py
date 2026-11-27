"""What an agent's detail view and peek panel show (AGX-07 §5.1–§5.3).

Everything here is read off the event loop: the bounded, redacted tail of the
agent's journal, its latest input receipt, its pending inbox request and, for
an agent in a worktree, the summary of what it changed.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agenthicc.background.inbox import BackgroundInboxItem
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.vocabulary import report_sentence

__all__ = [
    "SUBAGENT_ROLE",
    "TERMINAL_ROLE",
    "TRANSCRIPT_EVENTS",
    "ChangeSummary",
    "DetailData",
    "TranscriptLine",
    "approval_lines",
    "changes_summary",
    "journal_fingerprint",
    "read_transcript",
    "report_line",
]

#: Events kept in the transcript section (AGX-07 §5.3).
TRANSCRIPT_EVENTS = 50
#: The diff summary gives up after this long (AGX-07 §6: ≤ 300 ms budget, off loop).
DIFF_TIMEOUT_S = 2.0
#: Per-file stat lines kept from ``git diff --stat``.
DIFF_FILES = 12


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One journal event as one line: who, a status glyph and short text."""

    role: str
    text: str
    status: str = ""


@dataclass(frozen=True, slots=True)
class ChangeSummary:
    """``git diff --stat`` from the agent's base commit, uncommitted work included."""

    files: int = 0
    insertions: int = 0
    deletions: int = 0
    branch: str = ""
    base: str = ""
    lines: tuple[str, ...] = ()
    error: str = ""

    @property
    def headline(self) -> str:
        if self.error:
            return self.error
        if not self.files:
            return "No changes"
        plural = "file" if self.files == 1 else "files"
        return f"{self.files} {plural} changed, +{self.insertions} −{self.deletions}"


@dataclass(frozen=True, slots=True)
class DetailData:
    """One agent's detail projection, read off the event loop."""

    session_id: str
    attempt: int
    transcript: tuple[TranscriptLine, ...] = ()
    #: (inode, size, mtime) of the journal the transcript was read from.
    fingerprint: tuple[int, int, int] = (0, 0, 0)
    receipt: str | None = None
    history: tuple[str, ...] = ()
    #: The agent's pending inbox request, when it has one.
    item: BackgroundInboxItem | None = None
    changes: ChangeSummary | None = None


def journal_path(session: BackgroundSession) -> Path:
    return Path(session.artifact_dir).expanduser() / "conversation.jsonl"


def journal_fingerprint(path: Path) -> tuple[int, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0, 0)
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


#: Characters of one message kept in the transcript. A longer one ends in a
#: marker that says so; the view wraps what is kept, so nothing is cropped.
MESSAGE_CHARS = 8_000
#: Characters of one tool call's summary, and of its first output line.
TOOL_CHARS = 1_500


def _message_text(text: str, limit: int = MESSAGE_CHARS) -> str:
    """A message with its paragraphs and line breaks kept and its blank runs collapsed.

    This used to keep only the first line, cut at 240 characters, so an agent's
    reply read as one clipped sentence. The text is now whole up to *limit*
    characters, and a longer one ends in a line that says it was cut.
    """
    lines: list[str] = []
    blank = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        if not line:
            blank = bool(lines)
            continue
        if blank:
            lines.append("")
            blank = False
        lines.append(line)
    kept = "\n".join(lines)
    if len(kept) <= limit:
        return kept
    return kept[:limit].rstrip() + f"\n… (cut at {limit:,} characters)"


def _payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


#: The subagent lifecycle events the session log carries, shown under one role.
_SUBAGENT_EVENTS = frozenset(
    {
        "subagent_pool_started",
        "subagent_worker_done",
        "subagent_worker_failed",
        "subagent_worker_stop_requested",
        "subagent_pool_done",
        "subagent_transcript_degraded",
    }
)
SUBAGENT_ROLE = "subagents"
#: The role of a background terminal's notice; it fits the transcript's role column.
TERMINAL_ROLE = "terminals"


def _count(payload: dict[str, object], key: str) -> int:
    value = payload.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _subagent_notice_line(source: str, payload: dict[str, object]) -> TranscriptLine | None:
    """The notice the parent agent received about its subagents.

    The text is the model-facing message; the summary is what the operator
    reads. Neither is something the operator typed, so it is not a ``you`` row.
    """
    text = _message_text(_payload_text(payload, "summary") or _payload_text(payload, "text"))
    if not text:
        return None
    glyph = {"subagent_worker": "✓", "subagent_question": "◆"}.get(source, "●")
    return TranscriptLine(SUBAGENT_ROLE, f"parent notified · {text}", glyph)


#: How a background terminal ended, as the status glyph of its notice.
_TERMINAL_GLYPHS = {"exited": "✓", "failed": "✗", "timed_out": "✗", "orphaned": "✗"}


def _terminal_notice_line(payload: dict[str, object]) -> TranscriptLine | None:
    """The notice the agent received that a background terminal ended (COMM-02).

    The summary says which terminal and how it ended; the text, with the process's
    output tail, is the model-facing message and is not repeated here. The line
    is not something the operator typed, so it is not a ``you`` row.
    """
    text = _message_text(_payload_text(payload, "summary") or _payload_text(payload, "text"))
    if not text:
        return None
    glyph = _TERMINAL_GLYPHS.get(_payload_text(payload, "state"), "●")
    return TranscriptLine(TERMINAL_ROLE, f"agent notified · {text.partition(chr(10))[0]}", glyph)


def _subagent_line(kind: str, payload: dict[str, object]) -> TranscriptLine | None:
    """One subagent lifecycle event as a line: who finished, how, and why one stopped."""
    if kind == "subagent_pool_started":
        workers = payload.get("workers")
        labels = (
            [
                " ".join(str(worker.get("label", "")).split())
                for worker in workers
                if isinstance(worker, dict)
            ]
            if isinstance(workers, list)
            else []
        )
        total = max(_count(payload, "total"), len(labels))
        names = ", ".join(label for label in labels[:6] if label)
        more = f" +{total - 6} more" if total > 6 else ""
        text = f"{total} worker{'s' if total != 1 else ''} started"
        return TranscriptLine(SUBAGENT_ROLE, f"{text} · {names}{more}" if names else text, "●")
    if kind in {"subagent_worker_done", "subagent_worker_failed"}:
        label = " ".join(_payload_text(payload, "label").split()) or "worker"
        progress = f"[{_count(payload, 'done')}/{_count(payload, 'total')}] {label}"
        if payload.get("ok", kind == "subagent_worker_done") is not False:
            first = _message_text(_payload_text(payload, "text")).partition("\n")[0]
            return TranscriptLine(
                SUBAGENT_ROLE, f"{progress} · {first}" if first else progress, "✓"
            )
        error = _message_text(_payload_text(payload, "error")).partition("\n")[0] or "failed"
        return TranscriptLine(SUBAGENT_ROLE, f"{progress} · {error}", "✗")
    if kind == "subagent_worker_stop_requested":
        mode = _payload_text(payload, "mode") or "stop"
        reason = " ".join(_payload_text(payload, "reason").split())
        source = _payload_text(payload, "source")
        worker = _payload_text(payload, "worker_id")[-8:]
        detail = " · ".join(part for part in (source, reason) if part)
        text = f"{worker or 'worker'} {mode} requested"
        return TranscriptLine(SUBAGENT_ROLE, f"{text} · {detail}" if detail else text, "●")
    if kind == "subagent_pool_done":
        succeeded, total, failed = (
            _count(payload, key) for key in ("succeeded", "total", "failed")
        )
        text = f"{succeeded}/{total} done" + (f" · {failed} unsuccessful" if failed else "")
        return TranscriptLine(SUBAGENT_ROLE, text, "✗" if failed or succeeded < total else "✓")
    failures = _count(payload, "transcript_write_failures")
    return TranscriptLine(SUBAGENT_ROLE, f"transcripts partial · {failures} writes failed", "✗")


def transcript_line(kind: str, payload: dict[str, object]) -> TranscriptLine | None:
    """One journal event as a transcript line, or ``None`` for bookkeeping events.

    Payloads are data (TUX-06): a tool's ``summary`` is plain text, and an
    older journal is brought to the current shape by the session-log loader's
    one-way migration first.
    """
    from agenthicc.tui.runtime.session_log import migrate_event_payload

    payload = migrate_event_payload(kind, payload)
    if kind == "user_message":
        from agenthicc.subagents.delivery import RUNTIME_NOTICE_SOURCES

        source = _payload_text(payload, "source")
        if source == "terminal_notice":
            return _terminal_notice_line(payload)
        if source in RUNTIME_NOTICE_SOURCES:
            return _subagent_notice_line(source, payload)
        text = _message_text(_payload_text(payload, "text"))
        return TranscriptLine("you", text) if text else None
    if kind in _SUBAGENT_EVENTS:
        return _subagent_line(kind, payload)
    if kind in ("text", "assistant_message"):
        text = _message_text(_payload_text(payload, "text"))
        return TranscriptLine("assistant", text) if text else None
    if kind == "tool_complete":
        name = _payload_text(payload, "name") or "tool"
        args = " ".join(_payload_text(payload, "summary").split())
        state = payload.get("state")
        success = payload.get("success", True) is not False
        if state == "waiting":
            status = "◆"
        elif state in ("running",):
            status = "●"
        else:
            status = "✓" if success else "✗"
        output = payload.get("output_lines")
        first = (
            next((" ".join(str(line).split()) for line in output if str(line).strip()), "")
            if isinstance(output, list)
            else ""
        )
        text = args.strip()[:TOOL_CHARS]
        if first:
            first = first[:TOOL_CHARS]
            text = f"{text} · {first}" if text else first
        return TranscriptLine(name, text, status)
    if kind == "file_modified":
        path = _payload_text(payload, "path")
        return TranscriptLine("edit", path, "✓") if path else None
    if kind == "error":
        message = _message_text(_payload_text(payload, "message"))
        return TranscriptLine("error", message, "✗") if message else None
    if kind == "question_wait_started":
        question = _message_text(
            _payload_text(payload, "question") or _payload_text(payload, "text")
        )
        return TranscriptLine("question", question or "waiting for an answer", "◆")
    if kind == "system":
        text = _message_text(_payload_text(payload, "text"))
        return TranscriptLine("system", text) if text else None
    return None


def read_transcript(
    path: Path, *, tail_bytes: int, limit: int = TRANSCRIPT_EVENTS
) -> tuple[TranscriptLine, ...]:
    """The last *limit* transcript lines from the journal's bounded tail, redacted.

    Only the final *tail_bytes* of the journal are read (AGX-07 §5.6), so the
    cost does not grow with the journal.
    """
    from agenthicc.tui.runtime.session_export import _Redactor

    try:
        size = path.stat().st_size
        start = max(0, size - tail_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(tail_bytes)
    except OSError:
        return ()
    if start:
        # The tail may begin inside a record; keep only complete ones.
        _partial, separator, remainder = raw.partition(b"\n")
        raw = remainder if separator else b""
    redactor = _Redactor()
    lines: list[TranscriptLine] = []
    for record_text in raw.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(record_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        line = transcript_line(
            str(record.get("kind", "")), payload if isinstance(payload, dict) else {}
        )
        if line is None:
            continue
        safe = str(redactor.value(line.text, "text"))
        lines.append(TranscriptLine(line.role, safe, line.status))
    return tuple(lines[-limit:])


def report_line(transcript: tuple[TranscriptLine, ...]) -> str:
    """The first sentence of the agent's latest message (AGX-06 §5.2)."""
    for line in reversed(transcript):
        if line.role == "assistant" and line.text:
            return report_sentence(line.text)
    return ""


def _git(cwd: str, *args: str) -> str:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        timeout=DIFF_TIMEOUT_S,
        check=True,
        env=environment,
    )
    return completed.stdout


def changes_summary(session: BackgroundSession) -> ChangeSummary | None:
    """What a worktree agent changed since its base commit, or ``None``.

    Uncommitted work is included (``git diff BASE`` against the working
    tree). Errors are reported in the summary rather than raised.
    """
    if not session.base_commit or not (session.branch or session.worktree_id):
        return None
    base = session.base_commit
    try:
        shortstat = _git(session.cwd, "diff", "--shortstat", base)
        stat = _git(session.cwd, "diff", "--stat=120", base)
    except (OSError, subprocess.SubprocessError) as exc:
        return ChangeSummary(
            branch=session.branch,
            base=base[:7],
            error=f"Changes unavailable: {type(exc).__name__}",
        )
    files = insertions = deletions = 0
    for part in shortstat.split(","):
        words = part.split()
        if len(words) < 2 or not words[0].isdigit():
            continue
        count = int(words[0])
        if "file" in words[1]:
            files = count
        elif words[1].startswith("insertion"):
            insertions = count
        elif words[1].startswith("deletion"):
            deletions = count
    stat_lines = [line.strip() for line in stat.splitlines() if "|" in line]
    return ChangeSummary(
        files=files,
        insertions=insertions,
        deletions=deletions,
        branch=session.branch,
        base=base[:7],
        lines=tuple(stat_lines[:DIFF_FILES]),
    )


#: Characters of one request value (a command, a path). The panel wraps it, and a
#: longer one ends in a marker, so the operator is never shown half a command
#: as if it were all of it.
REQUEST_CHARS = 4_000


def _compact(value: object, limit: int = REQUEST_CHARS) -> str:
    """A request value on as many lines as it has, spaces collapsed, never silently cut."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return _message_text(text, limit)


def approval_lines(
    item: BackgroundInboxItem, session: BackgroundSession | None = None
) -> list[tuple[str, str]]:
    """The exact pending request as label/value pairs, redacted (AGX-07 §5.3).

    The card shows the inbox item's canonical target — the tool, its input as
    the worker recorded it (already redacted), the workspace paths the
    approval is bound to — never raw provider arguments.
    """
    from agenthicc.tui.runtime.session_export import _Redactor

    redactor = _Redactor()
    target = dict(item.target)
    rows: list[tuple[str, str]] = []
    tool = str(target.get("tool_name") or item.kind)
    requester = target.get("requester")
    if isinstance(requester, dict) and isinstance(requester.get("label"), str):
        # A subagent worker asked (AGX-08 §5.5).
        kind = requester.get("agent_type")
        rows.append(
            (
                "from",
                f"{requester['label']} ({kind})" if isinstance(kind, str) else requester["label"],
            )
        )
    if item.kind == "approval":
        tool_input = target.get("tool_input")
        arguments = tool_input if isinstance(tool_input, dict) else {}
        primary: object = next(
            (
                arguments[key]
                for key in ("command", "cmd", "path", "file_path", "url", "query")
                if isinstance(arguments.get(key), str) and arguments[key]
            ),
            arguments or "",
        )
        rows.append((tool, _compact(redactor.value(primary, "tool_input"))))
        accesses = target.get("workspace_access")
        for access in accesses[:3] if isinstance(accesses, list) else []:
            if isinstance(access, dict):
                shown = access.get("display") or access.get("canonical") or ""
                operation = access.get("operation") or ""
                rows.append((str(operation or "path"), _compact(shown)))
        where = item.project_root or (session.cwd if session is not None else "")
        mode = session.mode_name if session is not None else ""
        if where or mode:
            rows.append(("in", f"{where}  ({mode} mode)" if where and mode else where or mode))
        options = target.get("mode_options")
        if isinstance(options, list) and options:
            rows.append(("modes", ", ".join(str(option) for option in options[:6])))
        reason = arguments.get("description") or arguments.get("reason")
        if isinstance(reason, str) and reason.strip():
            rows.append(("why", f"“{_compact(redactor.value(reason, 'text'))}”"))
        return rows
    raw_questions = target.get("questions")
    questions = (
        [item for item in raw_questions if isinstance(item, dict)]
        if isinstance(raw_questions, list)
        else []
    )
    numbered = len(questions) > 1
    for number, question in enumerate(questions[:8], 1):
        label = f"asks {number}" if numbered else "asks"
        rows.append((label, _compact(redactor.value(question.get("text", ""), "text"))))
        options = question.get("options")
        for option in options[:8] if isinstance(options, list) else []:
            rows.append(("", "• " + _compact(redactor.value(str(option), "text"))))
    if len(questions) > 8:
        rows.append(("", f"…and {len(questions) - 8} more questions (i to answer)"))
    if not rows:
        rows.append(("asks", tool))
    return rows


def needs_you_reason(session: BackgroundSession) -> str:
    """What a waiting agent without an inbox request is waiting for."""
    if session.status is SessionStatus.ORPHANED:
        return "Its worker stopped without finishing. Resume to continue from the last checkpoint."
    if session.status is SessionStatus.WAITING_APPROVAL:
        # Without an inbox request (recorded before the inbox existed) the
        # recorded activity says the most about what waits.
        return session.latest_activity or session.approval_request or "Waiting for approval"
    if session.status is SessionStatus.WAITING_INPUT:
        return session.input_request or session.latest_activity or "Waiting for input"
    return ""
