"""Observe the session tool generation at actual provider request boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from lauren_ai._transport import Completion, CompletionChunk
from lauren_ai._transport._mock import MockTransport

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.kernel import AppState, Event, root_reducer
from agenthicc.plugins.trust import current_extension_trust
from agenthicc.subagents.tool import SubagentToolSelection, make_spawn_subagents_tool
from agenthicc.subagents.transcripts import list_pools
from agenthicc.tools.base import tool_name
from agenthicc.tools.surface import ToolSurface
from agenthicc.workflows.code_plan.runner import CodePlanRunner

from .test_workflow_stack_session import _reply, _settle
from .test_workflow_stack_session import stack_session as stack_session

if TYPE_CHECKING:
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.runners.tui_session import TUISession

pytestmark = pytest.mark.integration


def _write_trusted_tool() -> None:
    path = Path(".agenthicc/tools/live_probe.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from lauren_ai import tool\n"
        "from agenthicc.tools.capabilities import tool_read\n"
        "@tool_read\n"
        "@tool()\n"
        "async def live_probe() -> str:\n"
        '    """Read the live fixture."""\n'
        '    return "live generation result"\n'
        "TOOLS = [live_probe]\n",
        encoding="utf-8",
    )
    current_extension_trust().trust(path, kind="tool")


async def test_direct_turn_uses_new_schema_and_dispatch_after_reload(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, context, transport = stack_session
    old = context.reload.tools.current()
    _write_trusted_tool()
    names_at_request: list[set[str]] = []
    complete = transport.complete

    async def record(
        *args: object, **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        schemas = kwargs.get("tools")
        names_at_request.append(
            {str(item["name"]) for item in schemas if isinstance(item, dict)}
            if isinstance(schemas, list)
            else set()
        )
        return await complete(*args, **kwargs)

    monkeypatch.setattr(transport, "complete", record)
    events: list[Event] = []
    remove_sink = context.processor.add_sink(events.append)
    transport.queue_tool_use("reload_tools", {})
    transport.queue_tool_use("live_probe", {})
    _reply(transport, "Read the tool after the reload.")

    try:
        context.app_state.conversation.queue.append("Reload the tools and read the fixture.")
        session.advance()
        await _settle(session)
        await context.processor.drain()
    finally:
        remove_sink()

    first, following, _last = names_at_request
    assert "reload_tools" in first
    assert "live_probe" not in first
    assert "live_probe" in following
    assert "live generation result" in repr(transport.calls[-1].messages)
    assert context.reload.tools.current().generation > old.generation
    replay = AppState.create()
    schemas: list[set[str]] = []
    for event in events:
        if event.event_type in {"IntentCreated", "TurnToolSurfaceSelected"}:
            replay, _ = root_reducer(replay, event)
        if event.event_type == "TurnToolSurfaceSelected":
            intent = replay.intents[str(event.payload["intent_id"])]
            select = intent.metadata["tool_names"]
            assert isinstance(select, list)
            schemas.append(set(select))
    assert schemas == [first, following]


async def test_workers_hold_the_tool_generation_selected_at_spawn(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session, context, transport = stack_session
    assert context.session_conversation is not None
    journal = context.session_conversation.memory.journal
    old_generation = context.reload.tools.current().generation
    read = next(tool for tool in AGENT_TOOLS if tool_name(tool) == "read_file")
    Path("fixture.txt").write_text("Pinned tools fixture.", encoding="utf-8")
    requests: list[set[str]] = []
    first_worker = asyncio.Event()
    resume_first = asyncio.Event()
    complete = transport.complete

    async def record(
        *args: object, **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        schemas = kwargs.get("tools")
        requests.append(
            {str(item["name"]) for item in schemas if isinstance(item, dict)}
            if isinstance(schemas, list)
            else set()
        )
        if len(requests) == 1:
            first_worker.set()
            await resume_first.wait()
            transport.queue_tool_use("read_file", {"path": "fixture.txt"})
        else:
            _reply(transport, "The worker completed.")
        return await complete(*args, **kwargs)

    def select() -> SubagentToolSelection:
        active = context.reload.tools.current()
        return SubagentToolSelection(
            ToolSurface(active.generation, (read, *active.tools), active.sources)
        )

    monkeypatch.setattr(transport, "complete", record)
    spawn = make_spawn_subagents_tool(
        context.agent_runner,
        "mock-model",
        select,
        app_state=context.app_state,
        processor=context.processor,
        registry=context.subagent_registry,
        conversation_id=context.session_id,
        conversation_journal=journal,
    )

    async def work(task: str) -> dict[str, object]:
        return await cast(
            "Awaitable[dict[str, object]]", spawn(tasks=[{"type": "researcher", "task": task}])
        )

    async with asyncio.TaskGroup() as group:
        before = group.create_task(work("Check the first generation."))
        async with asyncio.timeout(5):
            await first_worker.wait()
            _write_trusted_tool()
            report = await context.reload.reload(frozenset({"tools"}), reason="api")
            after = await work("Check the next generation.")
        resume_first.set()

    assert report.ok and report.generation > old_generation
    assert before.result()["ok"] is True
    assert after["ok"] is True
    first, updated, continued = requests
    assert "live_probe" in updated
    assert "live_probe" not in first | continued
    manifests = list_pools(journal.path.parent / "subagents")
    generations = {worker.tool_surface_generation for pool in manifests for worker in pool.workers}
    assert generations == {old_generation, report.generation}


async def test_code_plan_reads_the_reloaded_catalog_at_the_next_phase(
    stack_session: tuple[TUISession, SessionContext, MockTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, context, transport = stack_session
    runner = CodePlanRunner(session._wf_config_base, mode_manager=context.mode_manager)
    requests: list[set[str]] = []
    complete = transport.complete

    async def record(
        *args: object, **kwargs: object
    ) -> Completion | AsyncIterator[CompletionChunk]:
        schemas = kwargs.get("tools")
        requests.append(
            {str(item["name"]) for item in schemas if isinstance(item, dict)}
            if isinstance(schemas, list)
            else set()
        )
        if len(requests) == 1:
            _write_trusted_tool()
            report = await context.reload.reload(frozenset({"tools"}), reason="api")
            assert report.ok
        return await complete(*args, **kwargs)

    monkeypatch.setattr(transport, "complete", record)
    _reply(transport, "The first phase ran.")
    await runner.run_phase(intent="Inspect.", text="First phase.", system_prompt="Inspect.")
    _reply(transport, "The second phase ran.")
    await runner.run_phase(intent="Inspect.", text="Second phase.", system_prompt="Report.")

    first, following = requests
    assert "live_probe" not in first
    assert "live_probe" in following
