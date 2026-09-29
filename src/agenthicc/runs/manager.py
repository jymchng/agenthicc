"""Goal-run orchestration over the existing background supervisor."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agenthicc.background import BackgroundSession, BackgroundStore, BackgroundSupervisor

from .model import GoalRun, GoalRunStatus, RunAgentRecord, RunTaskRecord, RunWorktreeRecord
from .store import RunStore

if TYPE_CHECKING:
    from agenthicc.workflows.checkpoint import WorkflowCheckpoint


class RepositoryValidationError(ValueError):
    """The goal cannot start because its repository/configuration is invalid."""


class _Supervisor(Protocol):
    store: BackgroundStore

    def submit(
        self,
        *,
        intent: str,
        workflow_name: str = "",
        title: str = "",
        cwd: str | None = None,
        session_id: str | None = None,
        config_path: str | None = None,
        set_overrides: tuple[str, ...] = (),
        dangerously_skip_permissions: bool = False,
        set_secret_overrides: tuple[str, ...] = (),
        parent_session_id: str = "",
        role: str = "",
        task_id: str = "",
        worktree_id: str = "",
        branch: str = "",
        base_commit: str = "",
        run_id: str = "",
        detached_goal: bool = False,
    ) -> BackgroundSession: ...

    def cancel(self, session_id: str) -> BackgroundSession: ...

    def resume(self, session_id: str) -> BackgroundSession: ...

    def attach_foreground(self, session_id: str) -> BackgroundSession: ...


def inspect_repository(path: Path, *, require_git: bool = True) -> tuple[Path, str, str]:
    """Return ``(root, branch, HEAD)`` using Git's authoritative state."""

    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise RepositoryValidationError(f"repository path is not a directory: {resolved}")
    try:
        root_result = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        root = Path(root_result.stdout.strip()).resolve()
        branch = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return root, branch, head
    except (OSError, subprocess.CalledProcessError) as exc:
        if require_git:
            raise RepositoryValidationError(
                f"{resolved} is not a usable Git repository: {exc}"
            ) from exc
        return resolved, "", ""


class GoalRunManager:
    """Create, launch, project, and control durable goal runs."""

    def __init__(
        self,
        *,
        store: RunStore | None = None,
        supervisor: _Supervisor | None = None,
        require_git: bool = True,
    ) -> None:
        self.store = store or RunStore()
        self.supervisor = supervisor or BackgroundSupervisor()
        self.background_store = self.supervisor.store
        self.require_git = require_git

    def _validate_workflow(self, workflow_name: str) -> None:
        from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415

        registry = build_workflow_registry(load_external=True)
        if registry.get(workflow_name) is None:
            raise RepositoryValidationError(f"unknown workflow: {workflow_name}")

    def create(
        self,
        goal: str,
        *,
        repository: Path | str,
        workflow_name: str = "goal_flow",
        detached: bool = False,
        run_id: str | None = None,
    ) -> GoalRun:
        self._validate_workflow(workflow_name)
        root, branch, head = inspect_repository(Path(repository), require_git=self.require_git)
        return self.store.create(
            GoalRun.create(
                goal,
                repository=str(root),
                repository_root=str(root),
                workflow_name=workflow_name,
                detached=detached,
                run_id=run_id,
                base_commit=head,
                main_branch=branch,
            )
        )

    def start_detached(
        self,
        goal: str,
        *,
        repository: Path | str,
        workflow_name: str = "goal_flow",
        config_path: str | None = None,
        set_overrides: tuple[str, ...] = (),
        set_secret_overrides: tuple[str, ...] = (),
        dangerously_skip_permissions: bool = False,
    ) -> GoalRun:
        run = self.create(
            goal,
            repository=repository,
            workflow_name=workflow_name,
            detached=True,
        )
        session_id = uuid.uuid4().hex
        try:
            session = self.supervisor.submit(
                intent=run.goal,
                workflow_name=run.workflow_name,
                title=run.goal[:80],
                cwd=run.repository_root,
                session_id=session_id,
                config_path=config_path,
                set_overrides=set_overrides,
                set_secret_overrides=set_secret_overrides,
                dangerously_skip_permissions=dangerously_skip_permissions,
                run_id=run.run_id,
                role="main",
                detached_goal=True,
            )
        except Exception as exc:
            self.store.update(
                run.run_id,
                status=GoalRunStatus.FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
                completed_at=run.last_updated_at,
                exit_code=1,
            )
            raise
        updated = self.store.update(
            run.run_id,
            status={
                "queued": GoalRunStatus.RUNNING,
                # The supervisor returns STARTING while the child process is
                # crossing the launch boundary. Preserve the historical
                # product-level contract that an accepted detached run is
                # already RUNNING; the background session retains the finer
                # lifecycle state.
                "starting": GoalRunStatus.RUNNING,
                "running": GoalRunStatus.RUNNING,
                "completed": GoalRunStatus.COMPLETED,
                "failed": GoalRunStatus.FAILED,
                "cancelled": GoalRunStatus.CANCELLED,
                "orphaned": GoalRunStatus.LOST,
            }.get(session.status.value, GoalRunStatus.NEEDS_ATTENTION),
            started_at=session.started_at or session.created_at,
            main_session_id=session.session_id,
            main_agent_id=session.session_id,
            worker_pid=session.worker_pid,
            worker_started_at=session.worker_started_at,
            worker_finished_at=session.worker_finished_at,
            worker_exit_code=session.worker_exit_code,
            worker_exit_reason=session.worker_exit_reason,
            worker_finalization_attempts=session.worker_finalization_attempts,
            worker_cleanup_error=session.worker_cleanup_error,
            exit_code=session.worker_exit_code,
            failure_reason=session.error or "",
        )
        self.store.link_agent(
            updated.run_id,
            RunAgentRecord(
                agent_id=session.session_id,
                run_id=updated.run_id,
                role="main",
                session_id=session.session_id,
                process_id=session.worker_pid,
                status=session.status.value,
                started_at=session.started_at,
                last_heartbeat_at=session.last_active,
                last_activity_at=session.last_active,
                current_phase=session.current_phase,
                exit_code=session.worker_exit_code,
                exit_reason=session.worker_exit_reason,
            ),
        )
        return self.store.get(updated.run_id)

    def reconcile(self, run_id: str) -> GoalRun:
        run = self.store.get(run_id)
        if not run.main_session_id:
            return run
        if isinstance(self.supervisor, BackgroundSupervisor):
            self.supervisor.recover_stale()
        session_store = self.background_store
        try:
            session = session_store.get(run.main_session_id, include_deleted=True)
        except KeyError:
            return self.store.update(
                run_id,
                status=GoalRunStatus.NEEDS_ATTENTION,
                attention_reasons=("main background session is missing",),
            )
        children = [
            item
            for item in session_store.list(include_archived=True, include_deleted=False)
            if item.parent_session_id == run.main_session_id or item.run_id == run_id
        ]
        mapping = {
            "completed": GoalRunStatus.COMPLETED,
            "failed": GoalRunStatus.FAILED,
            "cancelled": GoalRunStatus.CANCELLED,
            "cancelling": GoalRunStatus.CANCELLED,
            "orphaned": GoalRunStatus.LOST,
            "waiting_approval": GoalRunStatus.WAITING,
            "waiting_input": GoalRunStatus.WAITING,
        }
        status = mapping.get(session.status.value, GoalRunStatus.RUNNING)
        if status == GoalRunStatus.COMPLETED and any(
            item.status.value
            in {
                "queued",
                "starting",
                "running",
                "waiting_approval",
                "waiting_input",
                "retrying",
                "cancelling",
            }
            for item in children
        ):
            status = GoalRunStatus.INTEGRATING
        elif status == GoalRunStatus.COMPLETED and any(
            item.status.value in {"failed", "orphaned"} for item in children
        ):
            status = GoalRunStatus.NEEDS_ATTENTION
        reasons = (session.error,) if session.error else ()
        attention_reasons = tuple(reason for reason in reasons if reason)
        if (
            run.status == status
            and run.completed_at == session.completed_at
            and run.failure_reason == (session.error or run.failure_reason)
            and run.attention_reasons == attention_reasons
            and run.worker_pid == session.worker_pid
            and run.worker_started_at == session.worker_started_at
            and run.worker_finished_at == session.worker_finished_at
            and run.worker_exit_code == session.worker_exit_code
            and run.worker_exit_reason == session.worker_exit_reason
            and run.worker_finalization_attempts == session.worker_finalization_attempts
            and run.worker_cleanup_error == session.worker_cleanup_error
        ):
            return run
        return self.store.update(
            run_id,
            status=status,
            completed_at=session.completed_at,
            failure_reason=session.error or run.failure_reason,
            attention_reasons=attention_reasons,
            worker_pid=session.worker_pid,
            worker_started_at=session.worker_started_at,
            worker_finished_at=session.worker_finished_at,
            worker_exit_code=session.worker_exit_code,
            worker_exit_reason=session.worker_exit_reason,
            worker_finalization_attempts=session.worker_finalization_attempts,
            worker_cleanup_error=session.worker_cleanup_error,
            exit_code=session.worker_exit_code
            if session.worker_exit_code is not None
            else run.exit_code,
        )

    @staticmethod
    def _manifest_projection(
        run: GoalRun,
    ) -> tuple[
        tuple[str, ...], tuple[RunTaskRecord, ...], tuple[RunWorktreeRecord, ...], tuple[str, ...]
    ]:
        """Fold PRD-203 manifests into bounded run-level projections."""

        from agenthicc.worktrees import ManifestStore  # noqa: PLC0415

        orchestration_ids: list[str] = []
        tasks: list[RunTaskRecord] = []
        worktrees: list[RunWorktreeRecord] = []
        attention: list[str] = []
        seen_tasks: set[str] = set()
        seen_worktrees: set[str] = set()
        for manifest in ManifestStore().list():
            if manifest.run_id != run.run_id and manifest.parent_session_id != run.main_session_id:
                continue
            orchestration_ids.append(manifest.orchestration_id)
            for task in manifest.tasks:
                if task.task_id in seen_tasks:
                    continue
                seen_tasks.add(task.task_id)
                result = task.result
                tasks.append(
                    RunTaskRecord(
                        task_id=task.task_id,
                        description=task.description,
                        dependencies=task.dependencies,
                        status=task.status.value,
                        worker_session_id=task.worker_session_id,
                        worktree_id=task.worktree_id,
                        result_status=result.status if result is not None else "",
                        result_summary=result.summary[:4_000] if result is not None else "",
                        error=task.error[:2_000],
                        updated_at=task.updated_at,
                    )
                )
                if task.status.value in {"failed", "blocked", "conflict"}:
                    attention.append(f"task {task.task_id}: {task.status.value}")
            for worktree in manifest.worktrees:
                if worktree.worktree_id in seen_worktrees:
                    continue
                seen_worktrees.add(worktree.worktree_id)
                worktrees.append(
                    RunWorktreeRecord(
                        worktree_id=worktree.worktree_id,
                        path=worktree.path,
                        branch=worktree.branch,
                        base_commit=worktree.base_commit,
                        owner_session_id=worktree.owner_session_id,
                        task_id=worktree.task_id,
                        status=worktree.status.value,
                        head_commit=worktree.head_commit,
                        dirty=worktree.dirty,
                        conflict_paths=worktree.conflict_paths,
                        last_error=worktree.last_error[:2_000],
                        updated_at=worktree.updated_at,
                    )
                )
                if worktree.status.value in {"orphaned", "conflict"}:
                    attention.append(f"worktree {worktree.worktree_id}: {worktree.status.value}")
                if not Path(worktree.path).is_dir():
                    attention.append(f"worktree {worktree.worktree_id}: path is missing")
            if manifest.status.value in {"reviewing", "recovering", "failed"}:
                attention.append(
                    f"orchestration {manifest.orchestration_id}: {manifest.status.value}"
                )
        return (
            tuple(dict.fromkeys(orchestration_ids)),
            tuple(tasks),
            tuple(worktrees),
            tuple(dict.fromkeys(attention)),
        )

    @staticmethod
    def _status_for_manifests(
        current: GoalRunStatus,
        manifest_statuses: tuple[str, ...],
        *,
        has_attention: bool,
    ) -> GoalRunStatus:
        """Apply deterministic integration precedence over session status."""

        if has_attention:
            return GoalRunStatus.NEEDS_ATTENTION
        if "integrating" in manifest_statuses:
            return GoalRunStatus.INTEGRATING
        if "verifying" in manifest_statuses:
            return GoalRunStatus.VERIFYING
        if "reviewing" in manifest_statuses or "recovering" in manifest_statuses:
            return GoalRunStatus.NEEDS_ATTENTION
        if current == GoalRunStatus.CREATED and manifest_statuses:
            return GoalRunStatus.PLANNING
        return current

    @staticmethod
    def _workflow_projection(run: GoalRun) -> tuple[str, str, str]:
        """Return ``(workflow_run_id, phase, attention_reason)`` from checkpoints."""

        if not run.main_session_id:
            return "", "", ""
        workflow_root = Path.home() / ".agenthicc" / "sessions" / run.main_session_id / "workflows"
        if not workflow_root.is_dir():
            return "", "", ""
        from agenthicc.runners.workflow_checkpoint_store import (  # noqa: PLC0415
            WorkflowCheckpointStore,
        )

        store = WorkflowCheckpointStore(run.main_session_id)
        checkpoints: list["WorkflowCheckpoint"] = []
        for checkpoint_id in store.list_run_ids():
            try:
                checkpoint = store.load(checkpoint_id)
            except ValueError:
                return "", "", f"workflow checkpoint {checkpoint_id}: invalid"
            if checkpoint is not None and checkpoint.workflow_name == run.workflow_name:
                checkpoints.append(checkpoint)
        if not checkpoints:
            return "", "", ""
        checkpoint = max(checkpoints, key=lambda item: item.updated_at)
        attention = ""
        if checkpoint.status in {"failed", "discarded"}:
            attention = f"workflow {checkpoint.run_id}: {checkpoint.status}"
        return checkpoint.run_id, checkpoint.current_phase or "", attention

    def cancel(self, run_id: str) -> GoalRun:
        run = self.store.get(run_id)
        if run.main_session_id:
            store = self.background_store
            supervisor = self.supervisor
            linked = [
                item
                for item in store.list(include_archived=True, include_deleted=False)
                if item.session_id == run.main_session_id
                or item.run_id == run_id
                or item.parent_session_id == run.main_session_id
            ]
            for session in linked:
                if session.status.value not in {"completed", "failed", "cancelled"}:
                    supervisor.cancel(session.session_id)
        return self.store.update(
            run_id,
            status=GoalRunStatus.CANCELLED,
            completed_at=time.time(),
            exit_code=130,
        )

    def resume(self, run_id: str) -> GoalRun:
        """Resume the existing main session after evidence reconciliation."""

        run = self.projection(run_id)
        if not run.main_session_id:
            raise ValueError("goal run has no main session")
        session = self.background_store.get(run.main_session_id)
        resumed = self.supervisor.resume(session.session_id)
        self.store.update(
            run_id,
            status=GoalRunStatus.RUNNING,
            failure_reason="",
            attention_reasons=(),
            started_at=resumed.started_at or time.time(),
        )
        return self.projection(run_id)

    def attach(self, run_id: str) -> BackgroundSession:
        """Perform the supervisor half of an owner-safe foreground handoff."""

        run = self.projection(run_id)
        if not run.main_session_id:
            raise ValueError("goal run has no main session")
        return self.supervisor.attach_foreground(run.main_session_id)

    def projection(self, run_id: str) -> GoalRun:
        """Return a run joined with authoritative background/worker records."""

        run = self.reconcile(run_id)
        workflow_run_id, workflow_phase, workflow_attention = self._workflow_projection(run)
        if workflow_run_id or workflow_attention:
            attention_reasons = tuple(
                dict.fromkeys(
                    [*run.attention_reasons, *([workflow_attention] if workflow_attention else [])]
                )
            )
            if run.workflow_run_id != workflow_run_id or run.attention_reasons != attention_reasons:
                run = self.store.update(
                    run.run_id,
                    workflow_run_id=workflow_run_id,
                    attention_reasons=attention_reasons,
                )
        from agenthicc.worktrees import ManifestStore  # noqa: PLC0415

        orchestration_ids, tasks, worktrees, manifest_attention = self._manifest_projection(run)
        if orchestration_ids or tasks or worktrees or manifest_attention:
            manifest_statuses = tuple(
                manifest.status.value
                for manifest in ManifestStore().list()
                if manifest.orchestration_id in orchestration_ids
            )
            attention_reasons = tuple(dict.fromkeys([*run.attention_reasons, *manifest_attention]))
            status = self._status_for_manifests(
                run.status,
                manifest_statuses,
                has_attention=bool(attention_reasons),
            )
            changes: dict[str, object] = {
                "orchestration_ids": tuple(
                    dict.fromkeys([*run.orchestration_ids, *orchestration_ids])
                ),
                "tasks": tasks,
                "worktrees": worktrees,
                "attention_reasons": attention_reasons,
                "status": status,
            }
            if (
                run.orchestration_ids != changes["orchestration_ids"]
                or run.tasks != tasks
                or run.worktrees != worktrees
                or run.attention_reasons != attention_reasons
                or run.status != status
            ):
                run = self.store.update(run.run_id, **changes)
        sessions = self.background_store.list(include_archived=True, include_deleted=True)
        worktree_by_id = {item.worktree_id: item for item in run.worktrees}
        linked = [
            item
            for item in sessions
            if item.run_id == run.run_id
            or (run.main_session_id and item.parent_session_id == run.main_session_id)
            or item.session_id == run.main_session_id
        ]
        for session in linked:
            worktree = worktree_by_id.get(session.worktree_id)
            agent = RunAgentRecord(
                agent_id=session.session_id,
                run_id=run.run_id,
                role=session.role
                or ("main" if session.session_id == run.main_session_id else "worker"),
                task_id=session.task_id,
                session_id=session.session_id,
                process_id=session.worker_pid,
                status=session.status.value,
                worktree_id=session.worktree_id,
                worktree_path=worktree.path
                if worktree is not None
                else (session.cwd if session.worktree_id else ""),
                branch=session.branch,
                base_commit=session.base_commit,
                head_commit=worktree.head_commit if worktree is not None else "",
                workflow_run_id=run.workflow_run_id,
                started_at=session.started_at,
                completed_at=session.completed_at,
                last_heartbeat_at=session.last_active,
                last_activity_at=session.last_active,
                current_phase=session.current_phase
                or (workflow_phase if session.session_id == run.main_session_id else ""),
                current_operation=session.latest_activity,
                attention_reason=(
                    session.error
                    or (worktree.last_error if worktree is not None else "")
                    or ("dirty worktree" if worktree is not None and worktree.dirty else "")
                ),
                exit_code=session.worker_exit_code,
                exit_reason=session.worker_exit_reason,
            )
            run = run.with_agent(agent)
        return run
