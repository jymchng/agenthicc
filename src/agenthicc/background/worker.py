"""Detached background worker entry point (PRD-141).

Workers are deliberately thin adapters around the existing headless session
and agent-turn runners.  They never implement a second tool or workflow loop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agenthicc.background.model import SessionStatus
from agenthicc.background.store import BackgroundStore, InvalidSessionTransition
from agenthicc.cli.context import CLIContext, CLIFlags


@dataclass(frozen=True)
class WorkerRequest:
    session_id: str
    workflow_name: str
    intent: str
    cwd: str
    config_path: str | None
    set_overrides: tuple[str, ...]
    dangerously_skip_permissions: bool
    wall_timeout_s: float = 0.0
    max_activity_bytes: int = 64_000
    source: str = "cli"

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "WorkerRequest":
        session_id = value.get("session_id")
        intent = value.get("intent")
        cwd = value.get("cwd")
        if not all(isinstance(item, str) and item for item in (session_id, intent, cwd)):
            raise ValueError("background request requires session_id, intent, and cwd")
        assert isinstance(session_id, str)
        assert isinstance(intent, str)
        assert isinstance(cwd, str)
        raw_overrides = value.get("set_overrides", ())
        overrides = (
            tuple(item for item in raw_overrides if isinstance(item, str))
            if isinstance(raw_overrides, (list, tuple))
            else ()
        )
        config_path = value.get("config_path")
        raw_wall_timeout = value.get("wall_timeout_s", 0.0)
        wall_timeout_s = (
            float(raw_wall_timeout)
            if isinstance(raw_wall_timeout, (int, float)) and not isinstance(raw_wall_timeout, bool)
            else 0.0
        )
        raw_activity_bytes = value.get("max_activity_bytes", 64_000)
        max_activity_bytes = (
            int(raw_activity_bytes)
            if isinstance(raw_activity_bytes, int) and not isinstance(raw_activity_bytes, bool)
            else 64_000
        )
        return cls(
            session_id=session_id,
            workflow_name=str(value.get("workflow_name", "")),
            intent=intent,
            cwd=cwd,
            config_path=config_path if isinstance(config_path, str) else None,
            set_overrides=overrides,
            dangerously_skip_permissions=bool(value.get("dangerously_skip_permissions", False)),
            wall_timeout_s=wall_timeout_s,
            max_activity_bytes=max_activity_bytes,
            source=str(value.get("source", "cli")),
        )


class BackgroundApprovalService:
    """Approval adapter that waits for an explicit manager decision."""

    def __init__(self, store: BackgroundStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    async def request_approval(self, req: object) -> object:
        from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415

        if getattr(req, "kind", "tool") == "questions":
            return await self.request_input(req)

        description = str(getattr(req, "tool_name", "approval request"))[:120]
        try:
            current = self.store.get(self.session_id)
            self.store.transition(
                self.session_id,
                SessionStatus.WAITING_APPROVAL,
                expected_status=current.status,
                approval_request=description,
                approval_decision=None,
                latest_activity=f"Waiting for approval: {description}",
            )
        except (KeyError, InvalidSessionTransition):
            return ApprovalResponse(allowed=False, message="background session is no longer active")
        while True:
            await asyncio.sleep(0.2)
            try:
                current = self.store.get(self.session_id, include_deleted=True)
            except KeyError:
                return ApprovalResponse(allowed=False, message="background session was removed")
            if current.status in {
                SessionStatus.CANCELLING,
                SessionStatus.CANCELLED,
                SessionStatus.DELETED,
            }:
                return ApprovalResponse(allowed=False, message="background session was cancelled")
            if current.approval_decision is None:
                continue
            allowed = current.approval_decision
            try:
                self.store.transition(
                    self.session_id,
                    SessionStatus.RUNNING,
                    expected_status=SessionStatus.WAITING_APPROVAL,
                    approval_request="",
                    approval_decision=None,
                    latest_activity="Approval granted" if allowed else "Approval denied",
                )
            except InvalidSessionTransition:
                return ApprovalResponse(allowed=False, message="approval state changed")
            return ApprovalResponse(allowed=allowed)

    async def request_input(self, req: object) -> object:
        """Wait for explicit input to a workflow ``ask_user`` request."""

        from agenthicc.tools.approval import ApprovalResponse  # noqa: PLC0415

        description = str(getattr(req, "tool_name", "input requested"))[:120]
        try:
            current = self.store.get(self.session_id)
            self.store.transition(
                self.session_id,
                SessionStatus.WAITING_INPUT,
                expected_status=current.status,
                input_request=description,
                input_value=None,
                latest_activity=f"Waiting for input: {description}",
            )
        except (KeyError, InvalidSessionTransition):
            return ApprovalResponse(allowed=False, message="background session is no longer active")
        while True:
            await asyncio.sleep(0.2)
            try:
                current = self.store.get(self.session_id, include_deleted=True)
            except KeyError:
                return ApprovalResponse(allowed=False, message="background session was removed")
            if current.status in {
                SessionStatus.CANCELLING,
                SessionStatus.CANCELLED,
                SessionStatus.DELETED,
            }:
                return ApprovalResponse(allowed=False, message="background session was cancelled")
            if current.input_value is None:
                continue
            answer = current.input_value
            try:
                self.store.transition(
                    self.session_id,
                    SessionStatus.RUNNING,
                    expected_status=SessionStatus.WAITING_INPUT,
                    input_request="",
                    input_value=None,
                    latest_activity="Input accepted",
                )
            except InvalidSessionTransition:
                return ApprovalResponse(allowed=False, message="input state changed")
            return ApprovalResponse(allowed=True, message=answer)

    def respond(self, allowed: bool, **kwargs: object) -> None:
        self.store.update(self.session_id, approval_decision=allowed)

    def provide_input(self, value: str) -> None:
        """Deliver input for callers that hold the worker-side adapter."""

        if not isinstance(value, str) or not value.strip():
            raise ValueError("Input must not be empty")
        current = self.store.get(self.session_id)
        if current.status != SessionStatus.WAITING_INPUT:
            raise InvalidSessionTransition("Session is not waiting for input")
        self.store.update(self.session_id, input_value=value[:8_000])

    def reset_turn_memory(self) -> None:
        return None


class BackgroundInputService(BackgroundApprovalService):
    """Named input boundary for integrations that do not need approvals."""

    async def request_input(self, req: object) -> object:
        return await super().request_input(req)


def _load_request(path: Path) -> WorkerRequest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("background request must be a JSON object")
    return WorkerRequest.from_mapping(raw)


async def _run_direct_turn(session: object, request: WorkerRequest) -> None:
    """Run a direct turn through the canonical agent-turn runner."""

    from agenthicc.runners.agent_turn import _run_agent_turn  # noqa: PLC0415

    agent_runner = getattr(session, "agent_runner", None)
    if agent_runner is None:
        raise RuntimeError("No LLM configured for background session")
    app_state = getattr(session, "app_state")
    cfg = getattr(session, "cfg")
    await _run_agent_turn(
        request.intent,
        agent_runner,
        getattr(session, "processor"),
        session_memory=getattr(session, "session_memory"),
        max_agent_turns=cfg.execution.max_agent_turns,
        conv_store=app_state.conversation,
        app_state=app_state,
        exec_cfg=cfg.execution,
        skills=getattr(session, "skills"),
        skill_permissions=cfg.agents.skill_permissions_for("default"),
        mention_cache=getattr(session, "mention_cache"),
        project_plugin_tools=list(getattr(getattr(session, "project_plugins"), "all_tools", ())),
        mcp_registry=getattr(session, "mcp_registry"),
        active_agent="default",
        completed_turns=0,
        approval_svc=getattr(session, "approval_svc"),
        memory_router=getattr(session, "memory_router"),
        semantic_index=getattr(session, "semantic_index"),
    )


async def run_worker(request: WorkerRequest, store: BackgroundStore) -> int:
    """Claim, execute, and finalize one background session."""

    lease = uuid.uuid4().hex
    session = None
    processor_task: asyncio.Task[object] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    heartbeat_stop = asyncio.Event()
    original_cwd = os.getcwd()
    try:
        os.chdir(request.cwd)
        claimed = store.claim(request.session_id, pid=os.getpid(), lease_token=lease)
        if claimed.status != SessionStatus.RUNNING:
            raise RuntimeError(f"Worker could not claim session: {claimed.status.value}")

        async def _heartbeat() -> None:
            while not heartbeat_stop.is_set():
                await asyncio.sleep(1.0)
                if heartbeat_stop.is_set():
                    return
                try:
                    store.heartbeat(
                        request.session_id,
                        lease_token=lease,
                        activity="Worker active",
                    )
                except (KeyError, InvalidSessionTransition):
                    return

        heartbeat_task = asyncio.create_task(_heartbeat(), name="background-heartbeat")
        from agenthicc.runners.headless import (  # noqa: PLC0415
            _HeadlessApprovalService,
            _close_headless_session,
            execute_workflow,
        )
        from agenthicc.runners.tui_session import _build_session_context  # noqa: PLC0415

        ctx = CLIContext(
            resume_id=request.session_id,
            config_path=request.config_path,
            set_overrides=request.set_overrides,
            flags=CLIFlags(dangerously_skip_permissions=request.dangerously_skip_permissions),
        )
        # A CLI-created job may be the first durable record for this session.
        # Register it only when no foreground metadata exists; resume must not
        # reset the original session's timestamps.
        from agenthicc.tui.runtime.session_log import register_session  # noqa: PLC0415

        metadata_path = (
            Path.home() / ".agenthicc" / "sessions" / request.session_id / "metadata.json"
        )
        if not metadata_path.exists():
            register_session(request.session_id, request.cwd, "")
        session = await _build_session_context(
            request.session_id,
            list(request.set_overrides),
            config_path=request.config_path,
            headless=True,
        )
        session.app_state.cli_flags = ctx.flags
        setattr(
            session,
            "approval_svc",
            _HeadlessApprovalService(request.dangerously_skip_permissions)
            if request.dangerously_skip_permissions
            else BackgroundApprovalService(store, request.session_id),
        )
        processor_task = asyncio.create_task(session.processor.run(), name="background-processor")
        await asyncio.sleep(0)

        async def _execute() -> tuple[SessionStatus, str | None, str]:
            if request.workflow_name:
                result = await execute_workflow(session, request.workflow_name, request.intent)
                status = (
                    SessionStatus.COMPLETED if result.status == "complete" else SessionStatus.FAILED
                )
                return status, result.error, f"Workflow {result.status}"
            await _run_direct_turn(session, request)
            await session.processor.drain()
            return SessionStatus.COMPLETED, None, "Turn complete"

        if request.wall_timeout_s > 0:
            status, error, activity = await asyncio.wait_for(_execute(), request.wall_timeout_s)
        else:
            status, error, activity = await _execute()
        current = store.get(request.session_id, include_deleted=True)
        if current.status == SessionStatus.RUNNING:
            store.transition(
                request.session_id,
                status,
                expected_status=SessionStatus.RUNNING,
                expected_lease_token=lease,
                error=error,
                latest_activity=activity,
                worker_pid=None,
                lease_token="",
            )
        heartbeat_stop.set()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        return 0 if status == SessionStatus.COMPLETED else 1
    except asyncio.CancelledError:
        try:
            current = store.get(request.session_id, include_deleted=True)
            if current.status == SessionStatus.RUNNING:
                store.transition(
                    request.session_id, SessionStatus.CANCELLED, error="Worker cancelled"
                )
        except (KeyError, InvalidSessionTransition):
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            current = store.get(request.session_id, include_deleted=True)
            if current.status == SessionStatus.RUNNING:
                store.transition(
                    request.session_id,
                    SessionStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    latest_activity="Worker failed",
                    worker_pid=None,
                    lease_token="",
                )
        except (KeyError, InvalidSessionTransition):
            pass
        return 1
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if processor_task is not None and session is not None:
            try:
                from agenthicc.runners.headless import _close_headless_session  # noqa: PLC0415

                await _close_headless_session(session, processor_task, None)
            except Exception:  # noqa: BLE001
                pass
        try:
            os.chdir(original_cwd)
        except OSError:  # pragma: no cover - only an unrecoverable cwd teardown
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agenthicc detached background worker")
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--store-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = _load_request(Path(args.request_file))
        return asyncio.run(run_worker(request, BackgroundStore(Path(args.store_root))))
    except Exception as exc:  # noqa: BLE001
        print(f"background worker error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
