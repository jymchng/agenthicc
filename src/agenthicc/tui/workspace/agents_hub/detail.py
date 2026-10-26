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


def _first_line(text: str, limit: int = 240) -> str:
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped[:limit]
    return ""


def _payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def transcript_line(kind: str, payload: dict[str, object]) -> TranscriptLine | None:
    """One journal event as a transcript line, or ``None`` for bookkeeping events."""
    if kind == "user_message":
        text = _first_line(_payload_text(payload, "text"))
        return TranscriptLine("you", text) if text else None
    if kind in ("text", "assistant_message"):
        text = _first_line(_payload_text(payload, "text"))
        return TranscriptLine("assistant", text) if text else None
    if kind == "tool_complete":
        name = _payload_text(payload, "name") or "tool"
        args = " ".join(_payload_text(payload, "args_str").split())
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
        text = args.strip()
        if first:
            text = f"{text} · {first}" if text else first
        return TranscriptLine(name, text[:240], status)
    if kind == "file_modified":
        path = _payload_text(payload, "path")
        return TranscriptLine("edit", path, "✓") if path else None
    if kind == "error":
        message = _first_line(_payload_text(payload, "message"))
        return TranscriptLine("error", message, "✗") if message else None
    if kind == "question_wait_started":
        question = _first_line(_payload_text(payload, "question") or _payload_text(payload, "text"))
        return TranscriptLine("question", question or "waiting for an answer", "◆")
    if kind == "system":
        text = _first_line(_payload_text(payload, "text"))
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


def _compact(value: object, limit: int = 240) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())[:limit]


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
    questions = target.get("questions")
    for question in questions[:4] if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        rows.append(("asks", _compact(redactor.value(question.get("text", ""), "text"))))
        options = question.get("options")
        if isinstance(options, list) and options:
            rows.append(("options", _compact(", ".join(str(option) for option in options[:8]))))
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
