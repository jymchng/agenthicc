"""Worker-session lookup and provenance helpers for PRD-229 surfaces."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .environment import (
    EnvironmentCommand,
    EnvironmentManifest,
    EnvironmentRunReceipt,
    EnvironmentTrustStore,
    WorktreeEnvironment,
)
from .manager import WorktreeManager
from .model import ParallelManifest, ParallelTask, WorktreeRecord
from .store import ManifestStore

if TYPE_CHECKING:
    from agenthicc.background.model import BackgroundSession
    from agenthicc.background.store import BackgroundStore


@dataclass(frozen=True, slots=True)
class WorkerEnvironmentBinding:
    manifest: ParallelManifest
    task: ParallelTask
    worktree: WorktreeRecord
    environment: WorktreeEnvironment
    environment_manifest: EnvironmentManifest


def find_worker_environment(
    session_id: str,
    *,
    manifests: ManifestStore | None = None,
    trust_store: EnvironmentTrustStore | None = None,
) -> WorkerEnvironmentBinding:
    """Resolve an exact session to its durable task/worktree identity."""

    store = manifests or ManifestStore()
    for manifest in store.list():
        task = next((item for item in manifest.tasks if item.worker_session_id == session_id), None)
        if task is None or not task.worktree_id:
            continue
        record = manifest.worktree(task.worktree_id)
        repository = Path(manifest.repository).expanduser().resolve()
        worktree_path = Path(record.path).expanduser().resolve()
        if Path(record.repository).expanduser().resolve() != repository:
            raise PermissionError("worker worktree belongs to a different repository")
        actual_worktrees = WorktreeManager(
            repository,
            root=worktree_path.parent,
        ).recover()
        registered = next(
            (item for item in actual_worktrees if Path(item.path).resolve() == worktree_path),
            None,
        )
        if registered is None or registered.branch != record.branch:
            raise PermissionError(
                "worker path is not the registered branch worktree; refusing environment action"
            )
        environment = WorktreeEnvironment(
            worktree_path,
            record.worktree_id,
            trust_store=trust_store,
        )
        parsed = environment.load()
        if parsed is None:
            raise FileNotFoundError("worker worktree has no .agenthicc/environment.toml manifest")
        return WorkerEnvironmentBinding(manifest, task, record, environment, parsed)
    raise KeyError(f"no worktree is linked to worker session {session_id}")


def append_environment_receipt(
    session_id: str,
    receipt: EnvironmentRunReceipt,
    *,
    store: BackgroundStore | None = None,
) -> BackgroundSession:
    """Persist only the safe bounded receipt on the worker session record."""

    from agenthicc.background import BackgroundStore as _BackgroundStore

    session_store = store or _BackgroundStore()
    current = session_store.get(session_id, include_deleted=True)
    receipts = (*current.environment_receipts, receipt.to_json())[-64:]
    activity = (
        "Environment setup succeeded" if receipt.kind == "setup" else "Environment action completed"
    )
    if receipt.result != "succeeded":
        activity = "Environment command failed"
    return session_store.update(
        session_id,
        environment_receipts=receipts,
        latest_activity=activity,
    )


def run_session_action(
    session_id: str,
    action_id: str,
    *,
    approved: bool,
    cancel_event: threading.Event | None = None,
    expected_manifest_digest: str | None = None,
    manifests: ManifestStore | None = None,
    sessions: BackgroundStore | None = None,
) -> EnvironmentRunReceipt:
    """Run a selected named action and append its redacted receipt."""

    binding = find_worker_environment(session_id, manifests=manifests)
    receipt = binding.environment.run_action(
        action_id,
        approved=approved,
        cancel_event=cancel_event,
        expected_manifest_digest=(expected_manifest_digest or binding.environment_manifest.digest),
    )
    append_environment_receipt(session_id, receipt, store=sessions)
    return receipt


def run_session_setup(
    session_id: str,
    *,
    approved: bool,
    cancel_event: threading.Event | None = None,
    manifests: ManifestStore | None = None,
    sessions: BackgroundStore | None = None,
) -> EnvironmentRunReceipt | None:
    """Run setup explicitly for an existing worker and persist its receipt."""

    binding = find_worker_environment(session_id, manifests=manifests)
    receipt = binding.environment.run_setup(
        approved=approved,
        cancel_event=cancel_event,
        expected_manifest_digest=binding.environment_manifest.digest,
    )
    if receipt is not None:
        append_environment_receipt(session_id, receipt, store=sessions)
    return receipt


def available_session_actions(
    session: BackgroundSession,
    *,
    manifests: ManifestStore | None = None,
) -> tuple[EnvironmentCommand, ...]:
    """Return action definitions only for the exact selected worker session."""

    actions, _, _ = available_session_actions_with_context(session, manifests=manifests)
    return actions


def available_session_actions_with_context(
    session: BackgroundSession,
    *,
    manifests: ManifestStore | None = None,
) -> tuple[tuple[EnvironmentCommand, ...], str, Path]:
    """Return actions, reviewed digest, and verified workspace path."""

    if session.role != "worker" or not session.worktree_id:
        return (), "", Path(session.cwd).expanduser().resolve()
    binding = find_worker_environment(session.session_id, manifests=manifests)
    return (
        binding.environment_manifest.actions,
        binding.environment_manifest.digest,
        Path(binding.worktree.path).expanduser().resolve(),
    )
