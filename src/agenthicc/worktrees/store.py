"""Atomic durable storage for parallel orchestration manifests."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .model import ParallelManifest


class ManifestNotFound(KeyError):
    """Raised when a requested orchestration has no durable manifest."""


class ManifestStore:
    """Persist one JSON manifest per parent session.

    Writes use a lock, a private temporary file, ``fsync`` and ``os.replace``.
    The old manifest remains readable if a process is killed during a write.
    Manifests are never automatically deleted: completed worktrees may be
    cleaned, but their audit record remains available for recovery and review.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or Path.home() / ".agenthicc" / "orchestrations").expanduser()
        self.lock_path = self.root / "manifests.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
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

    def _path(self, orchestration_id: str) -> Path:
        if not orchestration_id or Path(orchestration_id).name != orchestration_id:
            raise ValueError("invalid orchestration id")
        return self.root / f"{orchestration_id}.json"

    def get(self, orchestration_id: str) -> ParallelManifest:
        path = self._path(orchestration_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ManifestNotFound(orchestration_id) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"cannot read orchestration manifest {orchestration_id}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"orchestration manifest {orchestration_id} is not an object")
        return _manifest_from_mapping(raw)

    def save(self, manifest: ParallelManifest) -> ParallelManifest:
        path = self._path(manifest.orchestration_id)
        with self._lock():
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
            try:
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return manifest

    def create(self, manifest: ParallelManifest) -> ParallelManifest:
        with self._lock():
            path = self._path(manifest.orchestration_id)
            if path.exists():
                raise ValueError(f"orchestration already exists: {manifest.orchestration_id}")
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
            try:
                directory_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        return manifest

    def list(self) -> list[ParallelManifest]:
        if not self.root.exists():
            return []
        results: list[ParallelManifest] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                results.append(self.get(path.stem))
            except (ManifestNotFound, OSError, ValueError):
                continue
        return sorted(results, key=lambda item: (-item.updated_at, item.orchestration_id))

    def update(self, orchestration_id: str, **changes: object) -> ParallelManifest:
        current = self.get(orchestration_id)
        return self.save(current.evolve(**changes))


def _manifest_from_mapping(value: dict[str, object]) -> ParallelManifest:
    from .model import (  # noqa: PLC0415
        OrchestrationStatus,
        ParallelTask,
        TaskStatus,
        WorktreeRecord,
        WorkerResult,
    )

    raw_tasks = value.get("tasks", [])
    tasks: list[ParallelTask] = []
    if isinstance(raw_tasks, list):
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                continue
            raw_result = raw.get("result")
            result = None
            if isinstance(raw_result, dict):
                result = WorkerResult(
                    task_id=str(raw_result.get("task_id", "")),
                    worker_session_id=str(raw_result.get("worker_session_id", "")),
                    worktree_id=str(raw_result.get("worktree_id", "")),
                    base_commit=str(raw_result.get("base_commit", "")),
                    head_commit=str(raw_result.get("head_commit", "")),
                    commits=tuple(
                        item for item in raw_result.get("commits", []) if isinstance(item, str)
                    ),
                    changed_files=tuple(
                        item
                        for item in raw_result.get("changed_files", [])
                        if isinstance(item, str)
                    ),
                    clean=bool(raw_result.get("clean", False)),
                    status=str(raw_result.get("status", "incomplete")),
                    summary=str(raw_result.get("summary", "")),
                    tests=tuple(
                        item for item in raw_result.get("tests", []) if isinstance(item, str)
                    ),
                    error=str(raw_result.get("error", "")),
                )
            tasks.append(
                ParallelTask(
                    task_id=str(raw.get("task_id", "")),
                    description=str(raw.get("description", "")),
                    dependencies=tuple(
                        item for item in raw.get("dependencies", []) if isinstance(item, str)
                    ),
                    status=TaskStatus(str(raw.get("status", TaskStatus.PENDING.value))),
                    worker_session_id=str(raw.get("worker_session_id", "")),
                    worktree_id=str(raw.get("worktree_id", "")),
                    result=result,
                    error=str(raw.get("error", "")),
                    created_at=float(raw.get("created_at", 0.0)),
                    updated_at=float(raw.get("updated_at", 0.0)),
                )
            )
    raw_worktrees = value.get("worktrees", [])
    worktrees: list[WorktreeRecord] = []
    if isinstance(raw_worktrees, list):
        for raw in raw_worktrees:
            if isinstance(raw, dict):
                worktrees.append(WorktreeRecord.from_mapping(raw))
    try:
        status = OrchestrationStatus(str(value.get("status", OrchestrationStatus.PLANNING.value)))
    except ValueError:
        status = OrchestrationStatus.RECOVERING
    raw_verification = value.get("verification", [])
    verification = (
        tuple(item for item in raw_verification if isinstance(item, str))
        if isinstance(raw_verification, (list, tuple))
        else ()
    )
    raw_created = value.get("created_at", 0.0)
    raw_updated = value.get("updated_at", time.time())
    created_at = float(raw_created) if isinstance(raw_created, (int, float)) else 0.0
    updated_at = float(raw_updated) if isinstance(raw_updated, (int, float)) else time.time()
    return ParallelManifest(
        orchestration_id=str(value.get("orchestration_id", "")),
        parent_session_id=str(value.get("parent_session_id", "")),
        repository=str(value.get("repository", "")),
        parent_branch=str(value.get("parent_branch", "")),
        base_commit=str(value.get("base_commit", "")),
        status=status,
        tasks=tuple(tasks),
        worktrees=tuple(worktrees),
        created_at=created_at,
        updated_at=updated_at,
        last_error=str(value.get("last_error", "")),
        verification=verification,
    )
