"""Session-owned cleanup of a paused workflow authoring run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from lauren_ai._transport import Completion, CompletionChunk
from lauren_ai._transport._mock import MockTransport

from agenthicc.plugins.trust import current_extension_trust
from agenthicc.tui.runtime.commands import InterruptAgentCommand
from agenthicc.workflows.create_workflow.authoring import PENDING_MARKER_NAME

from .test_workflow_stack_session import _reply, _settle
from .test_workflow_stack_session import stack_session as stack_session

if TYPE_CHECKING:
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.tui_session import TUISession

pytestmark = pytest.mark.integration


async def test_abandoning_authoring_during_generation_discards_the_pending_package(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, context, transport = stack_session
    context.mode_manager.set_by_name("Yolo")
    path = Path.cwd() / ".agenthicc/workflows/abandon_fixture/runner.py"
    generation = asyncio.Event()

    complete = transport.complete

    async def pause_after_write(
        *args: object, **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        if path.is_file() and not generation.is_set():
            generation.set()
            await asyncio.Event().wait()
        return await complete(*args, **kwargs)

    monkeypatch.setattr(transport, "complete", pause_after_write)
    transport.queue_tool_use(
        "finalize_design",
        {"workflow_name": "abandon_fixture", "design": "Inspect one fixture."},
    )
    _reply(transport, "Designed.")
    transport.queue_tool_use("write_file", {"path": "runner.py", "content": "# partial design\n"})

    assert session.route("/workflow call create_workflow Author a workflow for the fixture.")
    async with asyncio.timeout(8):
        await generation.wait()
    assert (path.parent / PENDING_MARKER_NAME).is_file()
    assert context.workflow_registry.get("abandon_fixture") is None

    session.handle_interrupt(InterruptAgentCommand(disposition="pause"))
    await _settle(session)
    assert context.workflow_stack.top.status == "paused"
    _reply(transport, "The pending authoring run was abandoned.")
    assert session.route("/workflow return --abandon")
    await _settle(session)

    assert not path.parent.exists()
    assert not current_extension_trust().records()
    assert context.workflow_registry.get("abandon_fixture") is None
    assert context.workflow_stack.depth == 0
    assert "Status: abandoned" in repr(transport.calls[-1].messages)
