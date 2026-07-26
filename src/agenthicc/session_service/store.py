"""Durable event storage for the client-neutral session service."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Iterator

from .models import SessionEvent

__all__ = ["SessionEventStore"]


def _safe_session_id(session_id: str) -> str:
    cleaned = session_id.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or Path(cleaned).name != cleaned
        or Path(cleaned).is_absolute()
        or "\\" in cleaned
    ):
        raise ValueError("session_id must be a single relative identifier")
    return cleaned


class SessionEventStore:
    """Append-only JSONL store with fsync and replay support.

    The store contains service events only. Existing kernel and conversation
    artifacts remain owned by their current persistence layers; this store is
    the durable cursor/projection ledger for multi-client coordination.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or (Path.home() / ".agenthicc" / "session-service"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{_safe_session_id(session_id)}.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).exists()

    def append(self, event: SessionEvent) -> None:
        path = self.path_for(event.session_id)
        record = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_many(self, events: Iterable[SessionEvent]) -> None:
        materialized = list(events)
        if not materialized:
            return
        path = self.path_for(materialized[0].session_id)
        if any(event.session_id != materialized[0].session_id for event in materialized):
            raise ValueError("all events in one append_many call must share a session")
        with self._lock, path.open("a", encoding="utf-8") as handle:
            for event in materialized:
                handle.write(
                    json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())

    def iter_events(self, session_id: str, *, after_sequence: int = 0) -> Iterator[SessionEvent]:
        path = self.path_for(session_id)
        if not path.exists():
            return
        with self._lock, path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = SessionEvent.from_mapping(json.loads(line))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if event.sequence > after_sequence:
                    yield event

    def all_events(self, session_id: str) -> list[SessionEvent]:
        return list(self.iter_events(session_id))

    def delete(self, session_id: str) -> None:
        path = self.path_for(session_id)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def session_ids(self) -> list[str]:
        with self._lock:
            return sorted(path.stem for path in self.root.glob("*.jsonl"))

    def compact(self, session_id: str, *, before_sequence: int) -> int:
        """Drop old events and return the earliest retained sequence.

        Compaction is explicit because clients must receive a replay-gap when
        they request a cursor older than the retained history.
        """

        events = [
            event for event in self.all_events(session_id) if event.sequence >= before_sequence
        ]
        if not events:
            return 0
        path = self.path_for(session_id)
        temp = path.with_suffix(".jsonl.tmp")
        with self._lock, temp.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temp.replace(path)
        return events[0].sequence
