"""Typed durable goal-run models for PRD-204."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class GoalRunStatus(str, Enum):
    """Coarse product-level status derived from session/workflow evidence."""

    CREATED = "created"
    STARTING = "starting"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING = "waiting"
    NEEDS_ATTENTION = "needs_attention"
    INTEGRATING = "integrating"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


_TERMINAL = frozenset({GoalRunStatus.COMPLETED, GoalRunStatus.FAILED, GoalRunStatus.CANCELLED})


def _text(value: object, default: str = "", *, limit: int = 8_000) -> str:
    return value[:limit] if isinstance(value, str) else default


def _timestamp(value: object, default: float | None = None) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _strings(value: object, *, limit: int = 64) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item[:512] for item in value if isinstance(item, str))[:limit]


def _status(value: object) -> GoalRunStatus:
    if isinstance(value, GoalRunStatus):
        return value
    if isinstance(value, str):
        try:
            return GoalRunStatus(value)
        except ValueError:
            pass
    return GoalRunStatus.NEEDS_ATTENTION


@dataclass(frozen=True)
class RunAgentRecord:
    """A projection/link to a main or worker Agenthicc session."""

    agent_id: str
    run_id: str
    role: str = "worker"
    task_id: str = ""
    session_id: str = ""
    process_id: int | None = None
    workflow_run_id: str = ""
    status: str = "created"
    worktree_id: str = ""
    worktree_path: str = ""
    branch: str = ""
    base_commit: str = ""
    head_commit: str = ""
    started_at: float | None = None
    completed_at: float | None = None
    last_heartbeat_at: float | None = None
    last_activity_at: float | None = None
    current_phase: str = ""
    current_operation: str = ""
    attention_reason: str = ""
    exit_code: int | None = None
    exit_reason: str = ""
    attempt: int = 0
    heartbeat_stale: bool = False
    requested_mode_name: str | None = None
    mode_name: str = ""
    mode_application_status: str = "pending"
    mode_application_error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "role": self.role,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "process_id": self.process_id,
            "pid": self.process_id,
            "workflow_run_id": self.workflow_run_id,
            "status": self.status,
            "worktree_id": self.worktree_id,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_activity_at": self.last_activity_at,
            "current_phase": self.current_phase,
            "current_operation": self.current_operation,
            "attention_reason": self.attention_reason,
            "exit_code": self.exit_code,
            "exit_reason": self.exit_reason,
            "attempt": self.attempt,
            "heartbeat_stale": self.heartbeat_stale,
            "requested_mode_name": self.requested_mode_name,
            "mode_name": self.mode_name,
            "mode_application_status": self.mode_application_status,
            "mode_application_error": self.mode_application_error,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "RunAgentRecord":
        agent_id = _text(value.get("agent_id"))
        if not agent_id:
            raise ValueError("agent record requires agent_id")
        raw_pid = value.get("process_id", value.get("pid"))
        process_id = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
        raw_exit_code = value.get("exit_code")
        exit_code = (
            raw_exit_code
            if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
            else None
        )
        raw_attempt = value.get("attempt")
        attempt = (
            raw_attempt if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool) else 0
        )
        raw_heartbeat_stale = value.get("heartbeat_stale")
        heartbeat_stale = raw_heartbeat_stale if isinstance(raw_heartbeat_stale, bool) else False
        raw_requested_mode = value.get("requested_mode_name")
        mode_status = _text(value.get("mode_application_status"), "pending", limit=32)
        if mode_status not in {"pending", "applied", "failed"}:
            mode_status = "pending"
        mode_name = _text(value.get("mode_name"), limit=128)
        if mode_status != "applied" or not mode_name:
            mode_name = ""
            if mode_status == "applied":
                mode_status = "pending"
        return cls(
            agent_id=agent_id,
            run_id=_text(value.get("run_id")),
            role=_text(value.get("role"), "worker"),
            task_id=_text(value.get("task_id")),
            session_id=_text(value.get("session_id")),
            process_id=process_id,
            workflow_run_id=_text(value.get("workflow_run_id")),
            status=_text(value.get("status"), "created"),
            worktree_id=_text(value.get("worktree_id")),
            worktree_path=_text(value.get("worktree_path")),
            branch=_text(value.get("branch")),
            base_commit=_text(value.get("base_commit")),
            head_commit=_text(value.get("head_commit")),
            started_at=_timestamp(value.get("started_at")),
            completed_at=_timestamp(value.get("completed_at")),
            last_heartbeat_at=_timestamp(value.get("last_heartbeat_at")),
            last_activity_at=_timestamp(value.get("last_activity_at")),
            current_phase=_text(value.get("current_phase")),
            current_operation=_text(value.get("current_operation")),
            attention_reason=_text(value.get("attention_reason"), limit=2_000),
            exit_code=exit_code,
            exit_reason=_text(value.get("exit_reason"), limit=128),
            attempt=attempt,
            heartbeat_stale=heartbeat_stale,
            requested_mode_name=(
                _text(raw_requested_mode, limit=128)
                if isinstance(raw_requested_mode, str)
                else None
            ),
            mode_name=mode_name,
            mode_application_status=mode_status,
            mode_application_error=_text(value.get("mode_application_error"), limit=1_024),
        )


@dataclass(frozen=True)
class RunTaskRecord:
    """Bounded projection of a PRD-203 task in a goal run."""

    task_id: str
    description: str = ""
    dependencies: tuple[str, ...] = ()
    status: str = "pending"
    worker_session_id: str = ""
    worktree_id: str = ""
    result_status: str = ""
    result_summary: str = ""
    error: str = ""
    updated_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "dependencies": list(self.dependencies),
            "status": self.status,
            "worker_session_id": self.worker_session_id,
            "worktree_id": self.worktree_id,
            "result_status": self.result_status,
            "result_summary": self.result_summary,
            "error": self.error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "RunTaskRecord":
        task_id = _text(value.get("task_id"))
        if not task_id:
            raise ValueError("task record requires task_id")
        raw_result = value.get("result")
        result_status = _text(value.get("result_status"))
        result_summary = _text(value.get("result_summary"), limit=4_000)
        if isinstance(raw_result, Mapping):
            result_status = _text(raw_result.get("status"), result_status)
            result_summary = _text(raw_result.get("summary"), result_summary, limit=4_000)
        return cls(
            task_id=task_id,
            description=_text(value.get("description"), limit=4_000),
            dependencies=_strings(value.get("dependencies"), limit=64),
            status=_text(value.get("status"), "pending"),
            worker_session_id=_text(value.get("worker_session_id")),
            worktree_id=_text(value.get("worktree_id")),
            result_status=result_status,
            result_summary=result_summary,
            error=_text(value.get("error"), limit=2_000),
            updated_at=_timestamp(value.get("updated_at")),
        )


@dataclass(frozen=True)
class RunWorktreeRecord:
    """Bounded projection of one isolated Git worktree."""

    worktree_id: str
    path: str = ""
    branch: str = ""
    base_commit: str = ""
    owner_session_id: str = ""
    task_id: str = ""
    status: str = "unknown"
    head_commit: str = ""
    dirty: bool = False
    conflict_paths: tuple[str, ...] = ()
    last_error: str = ""
    updated_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "worktree_id": self.worktree_id,
            "path": self.path,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "owner_session_id": self.owner_session_id,
            "task_id": self.task_id,
            "status": self.status,
            "head_commit": self.head_commit,
            "dirty": self.dirty,
            "conflict_paths": list(self.conflict_paths),
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "RunWorktreeRecord":
        worktree_id = _text(value.get("worktree_id"))
        if not worktree_id:
            raise ValueError("worktree record requires worktree_id")
        return cls(
            worktree_id=worktree_id,
            path=_text(value.get("path")),
            branch=_text(value.get("branch")),
            base_commit=_text(value.get("base_commit")),
            owner_session_id=_text(value.get("owner_session_id")),
            task_id=_text(value.get("task_id")),
            status=_text(value.get("status"), "unknown"),
            head_commit=_text(value.get("head_commit")),
            dirty=bool(value.get("dirty", False)),
            conflict_paths=_strings(value.get("conflict_paths"), limit=128),
            last_error=_text(value.get("last_error"), limit=2_000),
            updated_at=_timestamp(value.get("updated_at")),
        )


@dataclass(frozen=True)
class GoalRun:
    """Durable product-level record for one goal invocation."""

    run_id: str
    goal: str
    repository: str
    repository_root: str
    base_commit: str = ""
    main_branch: str = ""
    status: GoalRunStatus = GoalRunStatus.CREATED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    last_updated_at: float = field(default_factory=time.time)
    main_session_id: str = ""
    main_agent_id: str = ""
    workflow_name: str = "goal_flow"
    workflow_run_id: str = ""
    detached: bool = False
    exit_code: int | None = None
    result_summary: str = ""
    failure_reason: str = ""
    attention_reasons: tuple[str, ...] = ()
    orchestration_ids: tuple[str, ...] = ()
    git_remote_name: str = ""
    provider: str = ""
    model: str = ""
    configuration_fingerprint: str = ""
    token_usage_summary: str = ""
    estimated_cost_summary: str = ""
    agents: tuple[RunAgentRecord, ...] = ()
    tasks: tuple[RunTaskRecord, ...] = ()
    worktrees: tuple[RunWorktreeRecord, ...] = ()
    # PRD-205 fields are appended to preserve positional construction of the
    # PRD-204 GoalRun model. They remain serialized by name.
    worker_pid: int | None = None
    worker_started_at: float | None = None
    worker_finished_at: float | None = None
    worker_exit_code: int | None = None
    worker_exit_reason: str = ""
    worker_finalization_attempts: int = 0
    worker_cleanup_error: str = ""

    @classmethod
    def create(
        cls,
        goal: str,
        *,
        repository: str,
        repository_root: str | None = None,
        workflow_name: str = "goal_flow",
        detached: bool = False,
        run_id: str | None = None,
        base_commit: str = "",
        main_branch: str = "",
        now: float | None = None,
    ) -> "GoalRun":
        cleaned = " ".join(goal.split()).strip()
        if not cleaned:
            raise ValueError("goal must not be empty")
        if not workflow_name.strip():
            raise ValueError("workflow_name must not be empty")
        timestamp = time.time() if now is None else now
        rid = run_id or f"run_{uuid.uuid4().hex}"
        if not rid.startswith("run_"):
            raise ValueError("run_id must start with 'run_'")
        root = repository_root or repository
        return cls(
            run_id=rid,
            goal=cleaned,
            repository=repository,
            repository_root=root,
            workflow_name=workflow_name,
            detached=detached,
            base_commit=base_commit,
            main_branch=main_branch,
            created_at=timestamp,
            last_updated_at=timestamp,
        )

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def evolve(self, **changes: object) -> "GoalRun":
        data = self.to_dict(include_agents=False)
        data.update(changes)
        data["last_updated_at"] = time.time()
        requested_agents = changes.get("agents", self.agents)
        data["agents"] = (
            [
                agent.to_dict() if isinstance(agent, RunAgentRecord) else agent
                for agent in requested_agents
            ]
            if isinstance(requested_agents, (list, tuple))
            else []
        )
        requested_tasks = changes.get("tasks", self.tasks)
        data["tasks"] = (
            [
                task.to_dict() if isinstance(task, RunTaskRecord) else task
                for task in requested_tasks
            ]
            if isinstance(requested_tasks, (list, tuple))
            else []
        )
        requested_worktrees = changes.get("worktrees", self.worktrees)
        data["worktrees"] = (
            [
                worktree.to_dict() if isinstance(worktree, RunWorktreeRecord) else worktree
                for worktree in requested_worktrees
            ]
            if isinstance(requested_worktrees, (list, tuple))
            else []
        )
        return self.from_mapping(data)

    def with_agent(self, agent: RunAgentRecord) -> "GoalRun":
        replaced = [item for item in self.agents if item.agent_id != agent.agent_id]
        replaced.append(agent)
        return self.evolve(agents=tuple(replaced))

    def to_dict(self, *, include_agents: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "run_id": self.run_id,
            "goal": self.goal,
            "repository": self.repository,
            "repository_root": self.repository_root,
            "base_commit": self.base_commit,
            "main_branch": self.main_branch,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "last_updated_at": self.last_updated_at,
            "main_session_id": self.main_session_id,
            "main_agent_id": self.main_agent_id,
            "workflow_name": self.workflow_name,
            "workflow_run_id": self.workflow_run_id,
            "detached": self.detached,
            "worker_pid": self.worker_pid,
            "pid": self.worker_pid,
            "worker_started_at": self.worker_started_at,
            "worker_finished_at": self.worker_finished_at,
            "worker_exit_code": self.worker_exit_code,
            "worker_exit_reason": self.worker_exit_reason,
            "worker_finalization_attempts": self.worker_finalization_attempts,
            "worker_cleanup_error": self.worker_cleanup_error,
            "exit_code": self.exit_code,
            "result_summary": self.result_summary,
            "failure_reason": self.failure_reason,
            "attention_reasons": list(self.attention_reasons),
            "orchestration_ids": list(self.orchestration_ids),
            "attention": list(self.attention_reasons),
            "git_remote_name": self.git_remote_name,
            "provider": self.provider,
            "model": self.model,
            "configuration_fingerprint": self.configuration_fingerprint,
            "token_usage_summary": self.token_usage_summary,
            "estimated_cost_summary": self.estimated_cost_summary,
            "tasks": [item.to_dict() for item in self.tasks],
            "worktrees": [item.to_dict() for item in self.worktrees],
        }
        if include_agents:
            result["agents"] = [item.to_dict() for item in self.agents]
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "GoalRun":
        run_id = _text(value.get("run_id"))
        if not run_id:
            raise ValueError("goal run requires run_id")
        raw_agents = value.get("agents", [])
        agents: list[RunAgentRecord] = []
        if isinstance(raw_agents, (list, tuple)):
            for raw in raw_agents:
                if isinstance(raw, Mapping):
                    try:
                        agents.append(RunAgentRecord.from_mapping(raw))
                    except ValueError:
                        continue
        raw_tasks = value.get("tasks", [])
        tasks: list[RunTaskRecord] = []
        if isinstance(raw_tasks, (list, tuple)):
            for raw in raw_tasks:
                if isinstance(raw, Mapping):
                    try:
                        tasks.append(RunTaskRecord.from_mapping(raw))
                    except ValueError:
                        continue
        raw_worktrees = value.get("worktrees", [])
        worktrees: list[RunWorktreeRecord] = []
        if isinstance(raw_worktrees, (list, tuple)):
            for raw in raw_worktrees:
                if isinstance(raw, Mapping):
                    try:
                        worktrees.append(RunWorktreeRecord.from_mapping(raw))
                    except ValueError:
                        continue
        raw_exit = value.get("exit_code")
        exit_code = (
            raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        )
        raw_worker_pid = value.get("worker_pid", value.get("pid"))
        worker_pid = (
            raw_worker_pid
            if isinstance(raw_worker_pid, int) and not isinstance(raw_worker_pid, bool)
            else None
        )
        raw_worker_exit_code = value.get("worker_exit_code")
        worker_exit_code = (
            raw_worker_exit_code
            if isinstance(raw_worker_exit_code, int) and not isinstance(raw_worker_exit_code, bool)
            else None
        )
        raw_finalization_attempts = value.get("worker_finalization_attempts")
        finalization_attempts = (
            int(raw_finalization_attempts)
            if isinstance(raw_finalization_attempts, int)
            and not isinstance(raw_finalization_attempts, bool)
            else 0
        )
        return cls(
            run_id=run_id,
            goal=_text(value.get("goal")),
            repository=_text(value.get("repository")),
            repository_root=_text(value.get("repository_root")),
            base_commit=_text(value.get("base_commit")),
            main_branch=_text(value.get("main_branch")),
            status=_status(value.get("status")),
            created_at=_timestamp(value.get("created_at"), 0.0) or 0.0,
            started_at=_timestamp(value.get("started_at")),
            completed_at=_timestamp(value.get("completed_at")),
            last_updated_at=_timestamp(value.get("last_updated_at"), time.time()) or time.time(),
            main_session_id=_text(value.get("main_session_id")),
            main_agent_id=_text(value.get("main_agent_id")),
            workflow_name=_text(value.get("workflow_name"), "goal_flow"),
            workflow_run_id=_text(value.get("workflow_run_id")),
            detached=bool(value.get("detached", False)),
            worker_pid=worker_pid,
            worker_started_at=_timestamp(value.get("worker_started_at")),
            worker_finished_at=_timestamp(value.get("worker_finished_at")),
            worker_exit_code=worker_exit_code,
            worker_exit_reason=_text(value.get("worker_exit_reason"), limit=128),
            worker_finalization_attempts=finalization_attempts,
            worker_cleanup_error=_text(value.get("worker_cleanup_error"), limit=2_000),
            exit_code=exit_code,
            result_summary=_text(value.get("result_summary"), limit=4_000),
            failure_reason=_text(value.get("failure_reason"), limit=2_000),
            attention_reasons=_strings(
                value.get("attention_reasons", value.get("attention")), limit=32
            ),
            orchestration_ids=_strings(value.get("orchestration_ids"), limit=128),
            git_remote_name=_text(value.get("git_remote_name"), limit=256),
            provider=_text(value.get("provider"), limit=256),
            model=_text(value.get("model"), limit=256),
            configuration_fingerprint=_text(value.get("configuration_fingerprint"), limit=256),
            token_usage_summary=_text(value.get("token_usage_summary"), limit=1_000),
            estimated_cost_summary=_text(value.get("estimated_cost_summary"), limit=1_000),
            agents=tuple(agents),
            tasks=tuple(tasks),
            worktrees=tuple(worktrees),
        )
