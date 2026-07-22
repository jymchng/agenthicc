"""Portable, redacted exports for durable session artifacts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["export_session"]

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
    paths = _artifact_paths(valid_id, source_dir)
    if not any(path.exists() for path in paths.values()):
        raise FileNotFoundError(f"Session not found: {valid_id}")

    redactor = _Redactor()
    metadata, metadata_skipped = _read_json(paths["metadata"])
    jsonl_artifacts = {
        name: _read_jsonl(path)
        for name, path in paths.items()
        if name not in {"metadata", "cassette_metadata"}
    }
    cassette_metadata, cassette_metadata_skipped = _read_json(paths["cassette_metadata"])

    artifacts: dict[str, object] = {
        "kernel_events": [redactor.value(record) for record in jsonl_artifacts["kernel_events"][0]],
        "conversation_events": [
            redactor.value(record) for record in jsonl_artifacts["conversation_events"][0]
        ],
        "conversation_journal": [
            redactor.value(record) for record in jsonl_artifacts["conversation_journal"][0]
        ],
        "cassette": [redactor.value(record) for record in jsonl_artifacts["cassette"][0]],
        "approvals": [redactor.value(record) for record in jsonl_artifacts["approvals"][0]],
        "cassette_metadata": redactor.value(cassette_metadata),
    }
    manifest: dict[str, object] = {
        name: {
            "present": path.exists(),
            "records": len(jsonl_artifacts[name][0])
            if name in jsonl_artifacts
            else 1
            if path.exists()
            else 0,
            "skipped_lines": (
                jsonl_artifacts[name][1]
                if name in jsonl_artifacts
                else metadata_skipped
                if name == "metadata"
                else cassette_metadata_skipped
            ),
        }
        for name, path in paths.items()
    }
    document: dict[str, object] = {
        "format": _EXPORT_FORMAT,
        "schema_version": _EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session_id": valid_id,
        "metadata": redactor.value(metadata),
        "artifacts": artifacts,
        "manifest": manifest,
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
