"""Crash-tolerant local persistence for PRD-204 goal runs."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from .model import GoalRun, GoalRunStatus, RunAgentRecord


class RunNotFound(KeyError):
    """Raised when a run ID is not present in the local registry."""


def default_run_root() -> Path:
    return Path.home() / ".agenthicc" / "runs"


class RunStore:
    """Append-only JSONL store with locked, fsync'd writes."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or default_run_root()).expanduser()
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "runs.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            try:
                os.chmod(self.lock_path, 0o600)
            except OSError:
                pass
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

    def _read(self) -> list[dict[str, object]]:
        if not self.events_path.exists():
            return []
        result: list[dict[str, object]] = []
        try:
            with self.events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        result.append({str(key): item for key, item in value.items()})
        except OSError:
            return []
        return result

    def _append(self, event_type: str, payload: Mapping[str, object]) -> None:
        prior = self._read()
        sequence_numbers = [
            value
            for item in prior
            if isinstance((value := item.get("seq")), int) and not isinstance(value, bool)
        ]
        seq = max(sequence_numbers, default=0) + 1
        record = {
            "seq": seq,
            "event_type": event_type,
            "timestamp": time.time(),
            "payload": dict(payload),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _fold(self) -> dict[str, GoalRun]:
        runs: dict[str, GoalRun] = {}
        for event in self._read():
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            run_id = payload.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            kind = event.get("event_type")
            if kind == "run_created":
                raw = payload.get("run")
                if isinstance(raw, Mapping):
                    try:
                        runs[run_id] = GoalRun.from_mapping(raw)
                    except ValueError:
                        continue
            elif kind == "run_updated" and run_id in runs:
                raw_changes = payload.get("changes")
                if isinstance(raw_changes, Mapping):
                    try:
                        runs[run_id] = runs[run_id].evolve(**dict(raw_changes))
                    except (TypeError, ValueError):
                        continue
            elif kind == "agent_linked" and run_id in runs:
                raw_agent = payload.get("agent")
                if isinstance(raw_agent, Mapping):
                    try:
                        runs[run_id] = runs[run_id].with_agent(
                            RunAgentRecord.from_mapping(raw_agent)
                        )
                    except ValueError:
                        continue
            elif kind == "session_attempt_projected" and run_id in runs:
                raw_attempt = payload.get("attempt")
                session_id = payload.get("session_id")
                raw_agent = payload.get("agent")
                raw_changes = payload.get("changes")
                if (
                    not isinstance(raw_attempt, int)
                    or isinstance(raw_attempt, bool)
                    or not isinstance(session_id, str)
                    or not isinstance(raw_agent, Mapping)
                    or not isinstance(raw_changes, Mapping)
                ):
                    continue
                current = runs[run_id]
                previous = next(
                    (item for item in current.agents if item.session_id == session_id), None
                )
                if previous is not None and previous.attempt > raw_attempt:
                    continue
                try:
                    runs[run_id] = current.evolve(**dict(raw_changes)).with_agent(
                        RunAgentRecord.from_mapping(raw_agent)
                    )
                except (TypeError, ValueError):
                    continue
        return runs

    def create(self, run: GoalRun) -> GoalRun:
        with self._lock():
            if run.run_id in self._fold():
                raise ValueError(f"goal run already exists: {run.run_id}")
            self._append("run_created", {"run_id": run.run_id, "run": run.to_dict()})
        return run

    def get(self, run_id: str) -> GoalRun:
        run = self._fold().get(run_id)
        if run is None:
            raise RunNotFound(run_id)
        return run

    def list(
        self,
        *,
        status: GoalRunStatus | str | None = None,
        repository: str | None = None,
    ) -> list[GoalRun]:
        wanted = None
        if status is not None:
            wanted = status if isinstance(status, GoalRunStatus) else GoalRunStatus(status)
        values = list(self._fold().values())
        return sorted(
            [
                run
                for run in values
                if (wanted is None or run.status == wanted)
                and (repository is None or run.repository_root == repository)
            ],
            key=lambda item: (-item.last_updated_at, item.run_id),
        )

    def update(self, run_id: str, **changes: object) -> GoalRun:
        with self._lock():
            current = self.get(run_id)
            updated = current.evolve(**changes)
            serialized = updated.to_dict(include_agents=False)
            self._append(
                "run_updated",
                {"run_id": run_id, "changes": serialized},
            )
        return updated

    def link_agent(self, run_id: str, agent: RunAgentRecord) -> GoalRun:
        with self._lock():
            current = self.get(run_id)
            updated = current.with_agent(agent)
            self._append(
                "agent_linked",
                {"run_id": run_id, "agent": agent.to_dict()},
            )
        return updated

    def project_session_attempt(
        self,
        run_id: str,
        session_id: str,
        attempt: int,
        *,
        agent: RunAgentRecord,
        **changes: object,
    ) -> GoalRun:
        """Atomically project one session attempt unless a newer one won."""

        if not session_id or attempt < 0:
            raise ValueError("session attempt projection requires a valid identity")
        if agent.session_id != session_id or agent.attempt != attempt:
            raise ValueError("agent identity/attempt does not match session projection")
        with self._lock():
            current = self.get(run_id)
            previous = next(
                (item for item in current.agents if item.session_id == session_id), None
            )
            if previous is not None and previous.attempt > attempt:
                return current
            updated = current.evolve(**changes).with_agent(agent)
            serialized = updated.to_dict(include_agents=False)
            self._append(
                "session_attempt_projected",
                {
                    "run_id": run_id,
                    "session_id": session_id,
                    "attempt": attempt,
                    "changes": serialized,
                    "agent": agent.to_dict(),
                },
            )
            return updated
