"""Coordinator service for parallel worker sessions and integration."""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from agenthicc.background.model import BackgroundSession
from agenthicc.background.supervisor import BackgroundSupervisor

from .manager import IntegrationResult, WorktreeManager
from .model import (
    OrchestrationStatus,
    ParallelManifest,
    ParallelTask,
    TaskStatus,
    WorktreeRecord,
    WorktreeStatus,
    WorkerResult,
)
from .store import ManifestStore
from .environment import (
    EnvironmentRunReceipt,
    EnvironmentTrustStore,
    WorktreeEnvironment,
)


class CoordinatorError(RuntimeError):
    """The requested orchestration operation cannot be performed safely."""


class WorkerLauncher(Protocol):
    """The existing background supervisor contract used by worker dispatch."""

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
        environment_receipts: tuple[str, ...] = (),
    ) -> BackgroundSession: ...


def worker_prompt(description: str, *, worktree_path: str, base_commit: str) -> str:
    """Build the immutable worker contract injected into a worker session."""

    return (
        "[PARALLEL WORKER CONTRACT]\n"
        "You are an isolated worker. Work only on the assigned task below.\n"
        f"Assigned worktree: {worktree_path}\n"
        f"Assigned base commit: {base_commit}\n"
        "Do not access, modify, or switch to the coordinator worktree or another "
        "worker worktree. Do not change branches. Inspect and edit only the "
        "assigned worktree. Implement the task, run relevant validation, and "
        "commit the completed changes on the assigned branch. At completion, "
        "report the commit(s), tests, remaining risks, and a concise summary.\n\n"
        f"[ASSIGNED TASK]\n{description}"
    )


class ParallelCoordinator:
    """Own the parent manifest and delegate execution to existing jobs."""

    def __init__(
        self,
        repository: Path | str,
        *,
        parent_session_id: str,
        run_id: str = "",
        store: ManifestStore | None = None,
        manager: WorktreeManager | None = None,
        supervisor: WorkerLauncher | None = None,
        max_parallel_tasks: int = 4,
        # Preserve the original parallel_code_plan worker contract. Generic
        # workflow-wide dispatch explicitly selects the direct-turn worker
        # mode when it constructs this coordinator.
        worker_workflow_name: str = "code_plan",
        worker_dangerously_skip_permissions: bool = False,
        environment_trust_store: EnvironmentTrustStore | None = None,
    ) -> None:
        self.manager = manager or WorktreeManager(repository)
        self.store = store or ManifestStore()
        self.parent_session_id = parent_session_id
        self.run_id = run_id
        self.supervisor = supervisor
        if max_parallel_tasks < 1:
            raise ValueError("max_parallel_tasks must be at least 1")
        self.max_parallel_tasks = max_parallel_tasks
        self.worker_workflow_name = worker_workflow_name
        self.worker_dangerously_skip_permissions = worker_dangerously_skip_permissions
        self.environment_trust_store = environment_trust_store or EnvironmentTrustStore()

    def create(self, *, orchestration_id: str | None = None) -> ParallelManifest:
        """Create a manifest from a clean coordinator worktree."""

        self.manager.ensure_clean()
        orchestration_id = orchestration_id or uuid.uuid4().hex
        base = self.manager.head()
        manifest = ParallelManifest(
            orchestration_id=orchestration_id,
            parent_session_id=self.parent_session_id,
            run_id=self.run_id,
            repository=str(self.manager.repository),
            parent_branch=self.manager.branch(),
            base_commit=base,
            status=OrchestrationStatus.PLANNING,
        )
        return self.store.create(manifest)

    def manifest(self, orchestration_id: str) -> ParallelManifest:
        return self.store.get(orchestration_id)

    def add_task(
        self,
        orchestration_id: str,
        *,
        task_id: str,
        description: str,
        dependencies: tuple[str, ...] = (),
    ) -> ParallelManifest:
        manifest = self.manifest(orchestration_id)
        task_id = task_id.strip()
        if not task_id or not description.strip():
            raise CoordinatorError("task_id and description must not be empty")
        if any(item.task_id == task_id for item in manifest.tasks):
            raise CoordinatorError(f"task already exists: {task_id}")
        known = {item.task_id for item in manifest.tasks}
        unknown = [item for item in dependencies if item not in known]
        if unknown:
            raise CoordinatorError(f"unknown task dependencies: {', '.join(unknown)}")
        if task_id in dependencies:
            raise CoordinatorError("a task cannot depend on itself")
        task = ParallelTask(
            task_id=task_id, description=description.strip(), dependencies=dependencies
        )
        updated = manifest.evolve(
            tasks=[*manifest.tasks, task], status=OrchestrationStatus.PLANNING
        )
        if _has_cycle(updated):
            raise CoordinatorError("task dependency graph contains a cycle")
        return self.store.save(updated)

    def ready_tasks(self, orchestration_id: str) -> list[ParallelTask]:
        manifest = self.manifest(orchestration_id)
        completed = {
            item.task_id
            for item in manifest.tasks
            if item.status in {TaskStatus.COMPLETED, TaskStatus.INTEGRATED}
        }
        return [
            task
            for task in manifest.tasks
            if task.status == TaskStatus.PENDING and set(task.dependencies).issubset(completed)
        ]

    def dispatch(
        self,
        orchestration_id: str,
        task_id: str,
        *,
        environment_approved: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> ParallelTask:
        manifest = self.manifest(orchestration_id)
        task = manifest.task(task_id)
        if task.status != TaskStatus.PENDING:
            raise CoordinatorError(f"task {task_id} is not pending")
        if task not in self.ready_tasks(orchestration_id):
            raise CoordinatorError(f"task dependencies are not complete: {task_id}")
        if manifest.base_commit != self.manager.head():
            raise CoordinatorError("coordinator HEAD changed after the orchestration was created")
        # Fail before allocating a branch/worktree if project setup needs
        # approval. The exact command/workspace/network/timeout preview is
        # included in the actionable trust error.
        project_environment = WorktreeEnvironment(
            self.manager.repository, "preflight", trust_store=self.environment_trust_store
        )
        project_manifest = project_environment.load()
        planned_worktree_id = (
            task.worktree_id
            or uuid.uuid5(uuid.NAMESPACE_URL, f"agenthicc:{orchestration_id}:{task_id}").hex[:16]
        )
        planned_worktree_path = (
            Path(manifest.worktree(task.worktree_id).path)
            if task.worktree_id
            else self.manager.root / planned_worktree_id
        ).resolve()
        if project_manifest is not None and project_manifest.setup:
            project_environment.require_trust_or_approval(
                project_manifest,
                approved=environment_approved,
                workspace_root=planned_worktree_path,
            )
        record = (
            manifest.worktree(task.worktree_id)
            if task.worktree_id
            else self.manager.create(
                parent_session_id=self.parent_session_id,
                task_id=task.task_id,
                base_commit=manifest.base_commit,
                worktree_id=planned_worktree_id,
            )
        )
        if not Path(record.path).is_dir():
            raise CoordinatorError(f"worker worktree is missing: {record.path}")
        worker_session_id = uuid.uuid4().hex
        task = task.evolve(
            status=TaskStatus.DISPATCHING,
            worker_session_id=worker_session_id,
            worktree_id=record.worktree_id,
        )
        manifest = manifest.evolve(
            status=OrchestrationStatus.RUNNING,
            tasks=[task if item.task_id == task_id else item for item in manifest.tasks],
            worktrees=[record, *manifest.worktrees],
        )
        self.store.save(manifest)
        environment_receipt: EnvironmentRunReceipt | None = None
        try:
            worker_environment = WorktreeEnvironment(
                record.path,
                record.worktree_id,
                trust_store=self.environment_trust_store,
            )
            worker_manifest = worker_environment.load()
            if (project_manifest is None) != (worker_manifest is None) or (
                project_manifest is not None
                and worker_manifest is not None
                and project_manifest.digest != worker_manifest.digest
            ):
                raise CoordinatorError(
                    "worker worktree environment manifest differs from the reviewed repository "
                    "manifest; commit the manifest and retry dispatch"
                )
            environment_receipt = worker_environment.run_setup(
                approved=environment_approved,
                cancel_event=cancel_event,
                expected_manifest_digest=(
                    project_manifest.digest if project_manifest is not None else ""
                ),
            )
        except Exception as exc:  # setup failure is durable and leaves the worktree intact
            error = f"{type(exc).__name__}: {exc}"
            retryable_task = task.evolve(
                status=TaskStatus.PENDING,
                worker_session_id="",
                error=error,
            )
            self.store.save(
                manifest.evolve(
                    tasks=[
                        retryable_task if item.task_id == task_id else item
                        for item in manifest.tasks
                    ],
                    worktrees=[
                        record.evolve(last_error=error)
                        if item.worktree_id == record.worktree_id
                        else item
                        for item in manifest.worktrees
                    ],
                )
            )
            raise CoordinatorError(error) from exc
        if environment_receipt is not None:
            record = record.evolve(
                environment_receipts=(
                    *record.environment_receipts,
                    environment_receipt.to_json(),
                )[-64:]
            )
            manifest = manifest.evolve(
                worktrees=[
                    record if item.worktree_id == record.worktree_id else item
                    for item in manifest.worktrees
                ]
            )
            self.store.save(manifest)
        receipts = (environment_receipt.to_json(),) if environment_receipt is not None else ()
        if environment_receipt is not None and environment_receipt.result != "succeeded":
            command_detail = next(
                (
                    item.error or item.stderr.strip()
                    for item in environment_receipt.commands
                    if item.error or item.stderr.strip()
                ),
                "",
            )
            error = f"environment setup did not complete: {environment_receipt.result}"
            if command_detail:
                error += f" · {command_detail[:500]}"
            retryable_task = task.evolve(
                status=TaskStatus.PENDING,
                worker_session_id="",
                error=error,
            )
            self.store.save(
                manifest.evolve(
                    tasks=[
                        retryable_task if item.task_id == task_id else item
                        for item in manifest.tasks
                    ],
                    worktrees=[
                        record.evolve(last_error=error)
                        if item.worktree_id == record.worktree_id
                        else item
                        for item in manifest.worktrees
                    ],
                )
            )
            raise CoordinatorError(error)
        queued_dispatch = False
        if self.supervisor is not None:
            try:
                session = self.supervisor.submit(
                    intent=worker_prompt(
                        task.description,
                        worktree_path=record.path,
                        base_commit=record.base_commit,
                    ),
                    workflow_name=self.worker_workflow_name,
                    title=f"Worker {task.task_id}",
                    cwd=record.path,
                    session_id=worker_session_id,
                    dangerously_skip_permissions=self.worker_dangerously_skip_permissions,
                    parent_session_id=self.parent_session_id,
                    role="worker",
                    task_id=task.task_id,
                    worktree_id=record.worktree_id,
                    branch=record.branch,
                    base_commit=record.base_commit,
                    run_id=self.run_id,
                    environment_receipts=receipts,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.store.save(
                    manifest.evolve(
                        tasks=[
                            task.evolve(status=TaskStatus.FAILED, error=error)
                            if item.task_id == task_id
                            else item
                            for item in manifest.tasks
                        ],
                    )
                )
                raise CoordinatorError(error) from exc
            worker_session_id = session.session_id
            queued_dispatch = session.dispatch_state.value == "pending"
        running_task = task.evolve(
            status=TaskStatus.QUEUED if queued_dispatch else TaskStatus.RUNNING,
            worker_session_id=worker_session_id,
            worktree_id=record.worktree_id,
        )
        # Persist the post-launch identity.  Saving the pre-launch manifest
        # here leaves the task in ``dispatching`` forever, which causes the
        # collection phase to skip it after a restart.
        manifest = manifest.evolve(
            tasks=[running_task if item.task_id == task_id else item for item in manifest.tasks]
        )
        self.store.save(manifest)
        return running_task

    def inspect_task(self, orchestration_id: str, task_id: str) -> WorkerResult:
        manifest = self.manifest(orchestration_id)
        task = manifest.task(task_id)
        if not task.worktree_id:
            raise CoordinatorError(f"task has no worktree: {task_id}")
        record = manifest.worktree(task.worktree_id)
        worker_session_status = ""
        worker_session_error = ""
        if task.worker_session_id and isinstance(self.supervisor, BackgroundSupervisor):
            try:
                session = self.supervisor.store.get(
                    task.worker_session_id,
                    include_deleted=True,
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                session = None
            if isinstance(session, BackgroundSession):
                dispatch_state = session.dispatch_state.value
                session_status = session.status.value
                worker_session_status = session_status
                worker_session_error = session.error or ""
                if dispatch_state == "pending":
                    result = WorkerResult(
                        task_id=task.task_id,
                        worker_session_id=task.worker_session_id,
                        worktree_id=record.worktree_id,
                        base_commit=record.base_commit,
                        head_commit=record.base_commit,
                        status="queued",
                    )
                    self.store.save(
                        manifest.evolve(
                            tasks=[
                                task.evolve(status=TaskStatus.QUEUED, result=result)
                                if item.task_id == task_id
                                else item
                                for item in manifest.tasks
                            ]
                        )
                    )
                    return result
                if session_status in {
                    "starting",
                    "running",
                    "waiting_approval",
                    "waiting_input",
                    "retrying",
                    "cancelling",
                }:
                    result = WorkerResult(
                        task_id=task.task_id,
                        worker_session_id=task.worker_session_id,
                        worktree_id=record.worktree_id,
                        base_commit=record.base_commit,
                        head_commit=record.base_commit,
                        status="running",
                    )
                    self.store.save(
                        manifest.evolve(
                            tasks=[
                                task.evolve(status=TaskStatus.RUNNING, result=result)
                                if item.task_id == task_id
                                else item
                                for item in manifest.tasks
                            ]
                        )
                    )
                    return result
        result = self.manager.complete(record, worker_session_id=task.worker_session_id)
        from .model import TaskStatus, WorktreeStatus  # noqa: PLC0415

        if result.status == "complete":
            status = TaskStatus.COMPLETED
        elif worker_session_status in {"failed", "orphaned"}:
            status = TaskStatus.FAILED
            result = replace(
                result,
                status="worker_failed",
                error=worker_session_error or "Worker session failed",
            )
        elif worker_session_status == "cancelled":
            status = TaskStatus.CANCELLED
            result = replace(
                result,
                status="worker_cancelled",
                error=worker_session_error or "Worker was cancelled",
            )
        elif worker_session_status == "completed":
            status = TaskStatus.FAILED
            result = replace(
                result,
                status="incomplete",
                error="Worker completed without a clean committed result",
            )
        else:
            status = task.status
        self.store.save(
            manifest.evolve(
                tasks=[
                    task.evolve(status=status, result=result) if item.task_id == task_id else item
                    for item in manifest.tasks
                ],
                worktrees=[
                    record.evolve(
                        status=(
                            WorktreeStatus.COMPLETED
                            if status == TaskStatus.COMPLETED
                            else WorktreeStatus.ACTIVE
                        ),
                        head_commit=result.head_commit,
                        dirty=not result.clean,
                    )
                    if item.worktree_id == record.worktree_id
                    else item
                    for item in manifest.worktrees
                ],
            )
        )
        return result

    def integrate_task(self, orchestration_id: str, task_id: str) -> IntegrationResult:
        manifest = self.manifest(orchestration_id)
        task = manifest.task(task_id)
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.CONFLICT}:
            raise CoordinatorError(
                f"task must have completion evidence before integration: {task_id}"
            )
        record = manifest.worktree(task.worktree_id)
        if self.manager.branch() != manifest.parent_branch:
            raise CoordinatorError(
                "coordinator is not on the manifest parent branch; refusing integration"
            )
        self.store.save(
            manifest.evolve(
                status=OrchestrationStatus.INTEGRATING,
                tasks=[
                    task.evolve(status=TaskStatus.INTEGRATING) if item.task_id == task_id else item
                    for item in manifest.tasks
                ],
                worktrees=[
                    record.evolve(status=WorktreeStatus.INTEGRATING)
                    if item.worktree_id == record.worktree_id
                    else item
                    for item in manifest.worktrees
                ],
            )
        )
        result = self.manager.integrate(record, expected_branch=manifest.parent_branch)
        refreshed = self.store.get(orchestration_id)
        if result.ok:
            self.store.save(
                refreshed.evolve(
                    status=OrchestrationStatus.RUNNING,
                    tasks=[
                        task.evolve(status=TaskStatus.INTEGRATED)
                        if item.task_id == task_id
                        else item
                        for item in refreshed.tasks
                    ],
                    worktrees=[
                        record.evolve(
                            status=WorktreeStatus.INTEGRATED,
                            head_commit=result.head_commit,
                        )
                        if item.worktree_id == record.worktree_id
                        else item
                        for item in refreshed.worktrees
                    ],
                )
            )
        else:
            self.store.save(
                refreshed.evolve(
                    status=OrchestrationStatus.REVIEWING,
                    last_error=result.message,
                    tasks=[
                        task.evolve(status=TaskStatus.CONFLICT, error=result.message)
                        if item.task_id == task_id
                        else item
                        for item in refreshed.tasks
                    ],
                    worktrees=[
                        record.evolve(
                            status=WorktreeStatus.CONFLICT,
                            conflict_paths=result.conflict_paths,
                            last_error=result.message,
                        )
                        if item.worktree_id == record.worktree_id
                        else item
                        for item in refreshed.worktrees
                    ],
                )
            )
        return result

    def rebase_task(self, orchestration_id: str, task_id: str) -> IntegrationResult:
        manifest = self.manifest(orchestration_id)
        task = manifest.task(task_id)
        record = manifest.worktree(task.worktree_id)
        result = self.manager.rebase(record, onto=manifest.parent_branch)
        if result.ok:
            self.store.save(
                manifest.evolve(
                    tasks=[
                        task.evolve(status=TaskStatus.COMPLETED, error="")
                        if item.task_id == task_id
                        else item
                        for item in manifest.tasks
                    ],
                    worktrees=[
                        record.evolve(
                            status=WorktreeStatus.COMPLETED,
                            head_commit=result.head_commit,
                            conflict_paths=(),
                        )
                        if item.worktree_id == record.worktree_id
                        else item
                        for item in manifest.worktrees
                    ],
                )
            )
        return result

    def cleanup_task(self, orchestration_id: str, task_id: str) -> ParallelTask:
        manifest = self.manifest(orchestration_id)
        task = manifest.task(task_id)
        if task.status != TaskStatus.INTEGRATED:
            raise CoordinatorError("only integrated workers may be cleaned up")
        record = manifest.worktree(task.worktree_id)
        removed = self.manager.remove(record)
        task = task.evolve(status=TaskStatus.CLEANED)
        self.store.save(
            manifest.evolve(
                tasks=[task if item.task_id == task_id else item for item in manifest.tasks],
                worktrees=[
                    removed if item.worktree_id == record.worktree_id else item
                    for item in manifest.worktrees
                ],
            )
        )
        return task

    def recover(self, orchestration_id: str) -> ParallelManifest:
        manifest = self.manifest(orchestration_id)
        actual = {item.path: item for item in self.manager.recover()}
        worktrees: list[WorktreeRecord] = []
        for record in manifest.worktrees:
            if Path(record.path).is_dir():
                worktrees.append(record)
            else:
                worktrees.append(
                    record.evolve(
                        status=WorktreeStatus.ORPHANED,
                        last_error="worktree path is missing",
                    )
                )
        for record in actual.values():
            if not any(item.path == record.path for item in worktrees):
                worktrees.append(record)
        return self.store.save(
            manifest.evolve(status=OrchestrationStatus.RECOVERING, worktrees=worktrees)
        )

    async def dispatch_ready(self, orchestration_id: str) -> list[ParallelTask]:
        """Dispatch ready tasks without blocking the event loop."""

        ready = self.ready_tasks(orchestration_id)[: self.max_parallel_tasks]
        results: list[ParallelTask] = []
        for task in ready:
            cancel_event = threading.Event()
            dispatch_task = asyncio.create_task(
                asyncio.to_thread(
                    self.dispatch,
                    orchestration_id,
                    task.task_id,
                    cancel_event=cancel_event,
                )
            )
            try:
                results.append(await asyncio.shield(dispatch_task))
            except asyncio.CancelledError:
                # Cancelling asyncio.to_thread alone cannot stop its worker
                # thread. Signal the command runner, then wait for its process
                # group cleanup before propagating cancellation.
                cancel_event.set()
                try:
                    await asyncio.shield(dispatch_task)
                except Exception:  # noqa: BLE001 - preserve original cancellation
                    pass
                raise
        return results


def _has_cycle(manifest: ParallelManifest) -> bool:
    graph = {task.task_id: set(task.dependencies) for task in manifest.tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(dep) for dep in graph.get(node, ())):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)
