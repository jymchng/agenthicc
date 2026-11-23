"""Real TUI session setup and scripted provider lanes for subagent scenarios."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, CompletionChunk, Message
from lauren_ai._transport._mock import MockTransport
from rich.console import Console

from agenthicc.memory import journal
from agenthicc.runners import tui_session
from agenthicc.runners.headless import _close_headless_session
from agenthicc.runners.tui_session import TUISession
from agenthicc.testing.recording_transport import _tool_schema_name
from agenthicc.tui.input.unified_session import UnifiedInputSession
from agenthicc.tui.runtime import session_log
from agenthicc.tui.runtime.commands import SendMessageCommand
from agenthicc.tui.workspace import Workspace
from tests.e2e.self_extension.harness import ModelRequest, _capture_message, tool_call

WORKER_PREFIX = "Subagent script:"
WORKER_RESULT = "worker-only evidence"


@asynccontextmanager
async def tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "Yolo",
    config_text: str = "",
    resume_id: str | None = None,
) -> AsyncIterator[TUISession]:
    project = tmp_path / "project"
    test_home = tmp_path / "home"
    sessions = test_home / ".agenthicc" / "sessions"
    project.mkdir(exist_ok=True)
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", lambda: test_home)
    monkeypatch.setattr(journal, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(tui_session, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(session_log, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(session_log, "_SESSION_INDEX", sessions / "index.json")
    config = project / "agenthicc.toml"
    config.write_text("[skills]\ninstall_default_skills = false\n" + config_text, encoding="utf-8")
    context = await tui_session._build_session_context(
        resume_id, [], config_path=str(config), headless=True, mode_name=mode
    )
    context.console = Console(file=StringIO(), record=True, width=160)
    workspace = Workspace(context.app_state, context.console)
    input_session = UnifiedInputSession(context.app_state, context.command_bus)
    session = TUISession(context, workspace, input_session)
    task = asyncio.create_task(context.processor.run())
    try:
        yield session
    finally:
        session._stop_subagent_wakes()
        if session._agent_task is not None:
            session._agent_task.cancel()
            await asyncio.gather(session._agent_task, return_exceptions=True)
        await session._tasks.aclose()
        if context.startup is not None:
            await context.startup.close()
        await _close_headless_session(context, task, None)


@pytest.fixture
async def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TUISession]:
    async with tui(tmp_path, monkeypatch) as instance:
        yield instance


def spawn_call(count: int = 1, *, kind: str = "explorer", task: str = "inspect") -> Completion:
    return tool_call(
        "spawn_subagents",
        {
            "tasks": [
                {"type": kind, "task": f"{WORKER_PREFIX} {task} {index}"} for index in range(count)
            ],
            "max_concurrent": count,
        },
        use_id="spawn",
    )


class ScenarioTransport(MockTransport):
    """Route parent and worker requests independently of coroutine scheduling."""

    def __init__(
        self,
        main: Callable[[ModelRequest, int], Awaitable[Completion]],
        worker: Callable[[ModelRequest], Awaitable[Completion]],
    ) -> None:
        super().__init__()
        self.main = main
        self.worker = worker
        self.main_requests: list[ModelRequest] = []
        self.main_tool_schemas: list[str] = []
        self.worker_requests: list[ModelRequest] = []

    async def complete(
        self, messages: list[Message], **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        captured = tuple(_capture_message(message) for message in messages)
        data = kwargs.get("tools")
        system = kwargs.get("system")
        request = ModelRequest(
            str(kwargs["model"]),
            system if isinstance(system, str) else None,
            captured,
            tuple(_tool_schema_name(item) for item in data) if isinstance(data, list) else (),
        )
        if captured and WORKER_PREFIX in str(captured[0]["content"]):
            self.worker_requests.append(request)
            response = await self.worker(request)
        else:
            self.main_requests.append(request)
            self.main_tool_schemas.append(json.dumps(data, default=str, sort_keys=True))
            response = await self.main(request, len(self.main_requests))
        self.queue_response(response)
        return await super().complete(messages, **kwargs)


def attach(session: TUISession, transport: object) -> None:
    session._ctx.agent_runner = AgentRunnerBase(transport=transport)


async def send(session: TUISession, text: str) -> asyncio.Task[object]:
    await session.handle_send(SendMessageCommand(text=text))
    task = session._agent_task
    assert task is not None
    return task


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.001)
