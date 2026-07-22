"""Portable, redacted exports for durable session artifacts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["export_session", "inspect_session"]

_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"
_EXPORT_FORMAT = "agenthicc.session.export"
_EXPORT_SCHEMA_VERSION = 1
_REDACTED = "<redacted>"

_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "client_secret",
    "cookie",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "private_key",
    "refresh_token",
)
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
)


class _Redactor:
    def __init__(self) -> None:
        self.count = 0

    def value(self, value: object, key: str = "") -> object:
        normalized_key = key.lower().replace("-", "_")
        if self._sensitive_key(normalized_key):
            self.count += 1
            return _REDACTED
        if isinstance(value, Mapping):
            return {
                str(child_key): self.value(child, str(child_key))
                for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [self.value(child) for child in value]
        if isinstance(value, tuple):
            return [self.value(child) for child in value]
        if isinstance(value, str):
            return self._text(value)
        return value

    @staticmethod
    def _sensitive_key(key: str) -> bool:
        if key in _SENSITIVE_KEYS:
            return True
        return any(part in key for part in _SENSITIVE_KEY_PARTS)

    def _text(self, value: str) -> str:
        redacted = value
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(_REDACTED, redacted)
        if redacted != value:
            self.count += 1
        return redacted


@dataclass(frozen=True)
class _LoadedSession:
    paths: dict[str, Path]
    metadata: object
    metadata_skipped: int
    jsonl_artifacts: dict[str, tuple[list[object], int]]
    cassette_metadata: object
    cassette_metadata_skipped: int


def _validate_session_id(session_id: str) -> str:
    cleaned = session_id.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or Path(cleaned).name != cleaned
        or Path(cleaned).is_absolute()
        or "\\" in cleaned
    ):
        raise ValueError("session_id must be a single, relative session identifier")
    return cleaned


def _read_jsonl(path: Path) -> tuple[list[object], int]:
    records: list[object] = []
    skipped_lines = 0
    if not path.exists():
        return records, skipped_lines
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                skipped_lines += 1
    return records, skipped_lines


def _read_json(path: Path) -> tuple[object, int]:
    if not path.exists():
        return {}, 0
    try:
        return json.loads(path.read_text(encoding="utf-8")), 0
    except json.JSONDecodeError:
        return {}, 1


def _artifact_paths(session_id: str, sessions_dir: Path) -> dict[str, Path]:
    session_dir = sessions_dir / session_id
    return {
        "kernel_events": sessions_dir / f"{session_id}.jsonl",
        "metadata": session_dir / "metadata.json",
        "conversation_events": session_dir / "conversation.jsonl",
        "conversation_journal": session_dir / "conversation-journal.jsonl",
        "cassette": session_dir / "cassette" / "cassette.jsonl",
        "approvals": session_dir / "cassette" / "approvals.jsonl",
        "cassette_metadata": session_dir / "cassette" / "meta.json",
    }


def _load_session(session_id: str, sessions_dir: Path) -> _LoadedSession:
    paths = _artifact_paths(session_id, sessions_dir)
    if not any(path.exists() for path in paths.values()):
        raise FileNotFoundError(f"Session not found: {session_id}")

    jsonl_artifacts = {
        name: _read_jsonl(path)
        for name, path in paths.items()
        if name not in {"metadata", "cassette_metadata"}
    }
    metadata, metadata_skipped = _read_json(paths["metadata"])
    cassette_metadata, cassette_metadata_skipped = _read_json(paths["cassette_metadata"])
    return _LoadedSession(
        paths=paths,
        metadata=metadata,
        metadata_skipped=metadata_skipped,
        jsonl_artifacts=jsonl_artifacts,
        cassette_metadata=cassette_metadata,
        cassette_metadata_skipped=cassette_metadata_skipped,
    )


def _manifest(loaded: _LoadedSession) -> dict[str, object]:
    return {
        name: {
            "present": path.exists(),
            "records": len(loaded.jsonl_artifacts[name][0])
            if name in loaded.jsonl_artifacts
            else 1
            if path.exists()
            else 0,
            "skipped_lines": (
                loaded.jsonl_artifacts[name][1]
                if name in loaded.jsonl_artifacts
                else loaded.metadata_skipped
                if name == "metadata"
                else loaded.cassette_metadata_skipped
            ),
        }
        for name, path in loaded.paths.items()
    }


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _increment(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def _conversation_summary(records: list[object]) -> dict[str, object]:
    kinds: dict[str, int] = {}
    turns = 0
    tool_calls = 0
    errors = 0
    input_tokens = 0
    output_tokens = 0
    cost_usd = 0.0

    for record in records:
        entry = _mapping(record)
        if entry is None:
            _increment(kinds, "<invalid>")
            continue
        kind = _text(entry.get("kind"), "<unknown>")
        _increment(kinds, kind)
        if kind == "turn_start":
            turns += 1
        elif kind == "tool_complete":
            tool_calls += 1
        elif kind == "error":
            errors += 1
        if kind == "tokens":
            payload = _mapping(entry.get("payload"))
            if payload is not None:
                input_tokens += _integer(payload.get("input_tokens"))
                output_tokens += _integer(payload.get("output_tokens"))
                cost_usd += _number(payload.get("cost_usd"))

    return {
        "events": len(records),
        "by_kind": dict(sorted(kinds.items())),
        "turns": turns,
        "tool_calls": tool_calls,
        "errors": errors,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "cost_usd": cost_usd,
        },
    }


def _workflow_summary(records: list[object], redactor: _Redactor) -> dict[str, object]:
    runs: dict[str, dict[str, object]] = {}
    event_types: dict[str, int] = {}

    for record in records:
        entry = _mapping(record)
        if entry is None:
            continue
        event_type = _text(entry.get("event_type"), "<unknown>")
        if event_type not in {
            "WorkflowRunStarted",
            "WorkflowPhaseCompleted",
            "WorkflowRunCompleted",
        }:
            continue
        _increment(event_types, event_type)
        payload = _mapping(entry.get("payload"))
        if payload is None:
            continue
        run_id = _text(payload.get("run_id"))
        if not run_id:
            continue
        run = runs.setdefault(
            run_id,
            {
                "run_id": redactor.value(run_id),
                "workflow_name": "<unknown>",
                "status": "incomplete",
                "phases_run": 0,
            },
        )
        if event_type == "WorkflowRunStarted":
            workflow_name = _text(payload.get("workflow_name"), "<unknown>")
            run["workflow_name"] = redactor.value(workflow_name)
            phase_names = payload.get("phase_names")
            if isinstance(phase_names, list):
                run["phases_total"] = len(phase_names)
            run["status"] = "running"
        elif event_type == "WorkflowPhaseCompleted":
            run["phases_run"] = _integer(run.get("phases_run")) + 1
        else:
            status = _text(payload.get("status"), "incomplete")
            run["status"] = status
            if "phases_run" in payload:
                run["phases_run"] = _integer(payload.get("phases_run"))

    status_counts = {"complete": 0, "failed": 0, "exited": 0, "incomplete": 0}
    for run in runs.values():
        status = _text(run.get("status"), "incomplete")
        if status not in status_counts:
            status = "incomplete"
            run["status"] = status
        status_counts[status] += 1

    return {
        "events": dict(sorted(event_types.items())),
        "total": len(runs),
        "complete": status_counts["complete"],
        "failed": status_counts["failed"],
        "exited": status_counts["exited"],
        "incomplete": status_counts["incomplete"],
        "runs": list(runs.values()),
    }


def _resume_summary(records: list[object]) -> dict[str, object]:
    started: list[tuple[str, int]] = []
    completed: set[str] = set()
    tool_counts: dict[str, int] = {}

    for record in records:
        entry = _mapping(record)
        if entry is None:
            continue
        kind = _text(entry.get("kind"))
        turn_id = _text(entry.get("turn_id"))
        if kind == "turn_started" and turn_id:
            started.append((turn_id, _integer(entry.get("base_count"))))
        elif kind == "turn_completed" and turn_id:
            completed.add(turn_id)
        elif kind == "tool_recorded" and turn_id:
            _increment(tool_counts, turn_id)

    for turn_id, base_count in reversed(started):
        if turn_id not in completed:
            return {
                "incomplete": True,
                "turn_id": turn_id,
                "base_count": base_count,
                "tool_records": tool_counts.get(turn_id, 0),
                "turns_started": len(started),
                "turns_completed": len(completed),
            }
    return {
        "incomplete": False,
        "turn_id": None,
        "base_count": None,
        "tool_records": 0,
        "turns_started": len(started),
        "turns_completed": len(completed),
    }


def inspect_session(
    session_id: str,
    *,
    sessions_dir: str | Path | None = None,
) -> dict[str, object]:
    """Return a redacted health and usage summary for *session_id*.

    Inspection reads the same durable artifacts as :func:`export_session`,
    but returns aggregate information instead of conversation or tool
    payloads.  It reports malformed records, resume state, workflow status,
    and token/cost totals without exposing user messages or credentials.
    ``FileNotFoundError`` is raised when no session artifact exists.
    """
    valid_id = _validate_session_id(session_id)
    source_dir = Path(sessions_dir) if sessions_dir is not None else _SESSIONS_DIR
    loaded = _load_session(valid_id, source_dir)
    redactor = _Redactor()
    kernel_records = loaded.jsonl_artifacts["kernel_events"][0]
    conversation_records = loaded.jsonl_artifacts["conversation_events"][0]
    journal_records = loaded.jsonl_artifacts["conversation_journal"][0]

    return {
        "session_id": valid_id,
        "metadata": redactor.value(loaded.metadata),
        "artifacts": _manifest(loaded),
        "kernel": {
            "events": len(kernel_records),
            "by_type": dict(
                sorted(
                    _event_type_counts(kernel_records).items(),
                )
            ),
        },
        "conversation": _conversation_summary(conversation_records),
        "resume": _resume_summary(journal_records),
        "workflows": _workflow_summary(kernel_records, redactor),
        "redactions": redactor.count,
    }


def _event_type_counts(records: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        entry = _mapping(record)
        if entry is None:
            _increment(counts, "<invalid>")
        else:
            _increment(counts, _text(entry.get("event_type"), "<unknown>"))
    return counts


def export_session(
    session_id: str,
    output_path: str | Path,
    *,
    sessions_dir: str | Path | None = None,
) -> Path:
    """Write a portable, redacted JSON export for *session_id*.

    The export includes the canonical kernel event log, session metadata,
    reactive conversation events, durable conversation journal, and optional
    cassette artifacts. Missing optional artifacts are represented as empty
    collections. Invalid JSONL lines are skipped and counted in the manifest,
    which preserves the valid prefix of a session after a crash.

    The destination is written through a same-directory temporary file and an
    atomic replace. Existing destination files are replaced intentionally.
    ``FileNotFoundError`` is raised when no session artifact exists.
    """
    valid_id = _validate_session_id(session_id)
    source_dir = Path(sessions_dir) if sessions_dir is not None else _SESSIONS_DIR
    loaded = _load_session(valid_id, source_dir)
    redactor = _Redactor()

    artifacts: dict[str, object] = {
        "kernel_events": [
            redactor.value(record) for record in loaded.jsonl_artifacts["kernel_events"][0]
        ],
        "conversation_events": [
            redactor.value(record) for record in loaded.jsonl_artifacts["conversation_events"][0]
        ],
        "conversation_journal": [
            redactor.value(record) for record in loaded.jsonl_artifacts["conversation_journal"][0]
        ],
        "cassette": [redactor.value(record) for record in loaded.jsonl_artifacts["cassette"][0]],
        "approvals": [redactor.value(record) for record in loaded.jsonl_artifacts["approvals"][0]],
        "cassette_metadata": redactor.value(loaded.cassette_metadata),
    }
    document: dict[str, object] = {
        "format": _EXPORT_FORMAT,
        "schema_version": _EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session_id": valid_id,
        "metadata": redactor.value(loaded.metadata),
        "artifacts": artifacts,
        "manifest": _manifest(loaded),
        "redactions": redactor.count,
    }

    destination = Path(output_path)
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination
