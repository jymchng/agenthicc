"""Cassette-backed TUI scenario for non-blocking spawn and a complete wake."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lauren_ai._transport import Completion

from agenthicc.runners.tui_session import TUISession
from agenthicc.runners.usage_ledger import UsageCategory
from agenthicc.subagents.spawn_mode import BACKGROUND_PROMPT
from agenthicc.testing.cassette import SessionCassette
from agenthicc.testing.recording_transport import RecordingTransport
from tests.e2e.self_extension.harness import ModelRequest, reply, tool_call
from tests.integration.test_subagent_wake import notices
from tests.subagent_sessions import (
    WORKER_RESULT,
    ScenarioTransport,
    attach,
    send,
    spawn_call,
    tui,
    until,
)
from tests.subagent_sessions import session as session

pytestmark = pytest.mark.e2e


async def test_a2_a4_idle_wake_collects_in_a_full_recorded_tui_session(
    session: TUISession,
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    completed = asyncio.Event()
    Path("first.txt").write_text("first unrelated result", encoding="utf-8")
    Path("second.txt").write_text("second unrelated result", encoding="utf-8")
    answered = "Collected the worker's evidence."

    async def worker(_request: ModelRequest) -> Completion:
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        assert request.system and request.system.count(BACKGROUND_PROMPT) == 1
        if sequence == 1:
            return spawn_call()
        if sequence in (2, 3, 4):
            assert '"status": "running"' in repr(request.messages)
            assert not release.is_set()
            assert not notices(request)
            if sequence == 2:
                return tool_call("read_file", {"path": "first.txt"}, use_id="read-first")
            assert request.contains("first unrelated result")
            if sequence == 3:
                return tool_call("read_file", {"path": "second.txt"}, use_id="read-second")
            assert request.contains("second unrelated result")
            return reply("The two files are read. The delegated work is still running.")
        source = session._subagent_notices
        assert source is not None
        (handle,) = source.registry.handles()
        assert len(notices(request)) == 1
        assert (
            handle.pool_id in notices(request)[0]
            and "(1/1 workers of this pool have finished; the pool is complete)"
            in notices(request)[0]
        )
        if sequence == 5:
            assert request.messages[-1]["content"] == notices(request)[0]
            assert WORKER_RESULT not in repr(request.messages)
            user_input = [
                message
                for message in request.messages
                if message["role"] == "user" and isinstance(message["content"], str)
            ]
            assert len(user_input) == 2
            return tool_call(
                "collect_subagent_results", {"pool_id": handle.pool_id}, use_id="collect"
            )
        assert sequence == 6
        tool_exchange = request.messages[-1]
        assert WORKER_RESULT in str(tool_exchange["content"])
        assert "tool_result" in str(tool_exchange["content"])
        assert WORKER_RESULT not in notices(request)[0]
        completed.set()
        return reply(answered)

    cassette = tmp_path / "cassette.jsonl"
    script = ScenarioTransport(main, worker)
    attach(session, RecordingTransport(script, cassette))
    scroll = session._workspace.scroll
    scroll.mount()
    try:
        await (await send(session, "Read my files and delegate the investigation."))
        assert len(script.main_requests) == 4
        assert session._agent_task is None
        assert session._ctx.app_state.conversation.subagent_background_running() == 1
        assert "The two files are read." in session._ctx.console.export_text(clear=False)
        session._input_session.set_text("Unsubmitted text")
        release.set()
        await until(completed.is_set)
        await until(lambda: session._agent_task is None)
        scroll.flush()
    finally:
        scroll.unmount()
    assert "".join(session._ctx.app_state.input.buf()) == "Unsubmitted text"
    rendered = session._ctx.console.export_text(clear=False)
    assert rendered.count("↩ Subagent finished") == 1 and answered in rendered
    assert len(script.main_requests) == 6
    entries = SessionCassette.from_path(cassette).entries
    assert [name for entry in entries for name in entry.called_tool_names] == [
        "spawn_subagents",
        "read_file",
        "read_file",
        "collect_subagent_results",
    ]
    assert any(entry.response_content == answered for entry in entries)
    ledger = session._ctx.usage_ledger
    assert ledger.snapshot(category=UsageCategory.AGENT).calls == 4
    wakes = [record for record in ledger.records() if record.category == UsageCategory.WAKE]
    assert len(wakes) == 2 and all(record.run_id.startswith("subagent-wake:") for record in wakes)
    assert session.route("/usage")
    assert "Subagent wakes: 4 tokens (2 calls)" in session._ctx.console.export_text()


async def test_a18_foreground_turn_has_no_background_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def worker(_request: ModelRequest) -> Completion:
        return reply("The foreground result.")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        assert "spawn_subagents" in request.tools
        assert BACKGROUND_PROMPT not in (request.system or "")
        if sequence == 1:
            return spawn_call()
        assert request.contains("The foreground result.")
        return reply("Done.")

    async with tui(
        tmp_path, monkeypatch, config_text='[subagents]\nspawn_mode = "foreground"\n'
    ) as session:
        script = ScenarioTransport(main, worker)
        attach(session, script)
        await (await send(session, "Delegate in foreground."))
        assert len(script.main_requests) == 2 and len(script.worker_requests) == 1
        assert all(not notices(request) for request in script.main_requests)


async def test_a6_workflow_footer_and_scroll_survive_a_parked_phase(
    session: TUISession,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from rich.text import Text

    from agenthicc.tui.runtime.commands import InterruptAgentCommand
    from tests.integration.test_workflow_phase_subagents import pool_id, select_generate, source_for

    release = asyncio.Event()
    provider_resumed = asyncio.Event()
    finish = asyncio.Event()

    async def worker(_: ModelRequest) -> Completion:
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            assert request.contains('"status": "running"')
            return reply("The spawn returned. Waiting for phase results.")
        if sequence == 3:
            assert not notices(request)
            return reply("The worker is still running after resume.")
        if sequence == 4:
            provider_resumed.set()
            await finish.wait()
            assert len(notices(request)) == 1
            return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
        if sequence == 5:
            return tool_call("mark_execute_complete", {"summary": "Results accounted for."})
        assert sequence == 6
        return reply("The phase finished.")

    cassette = tmp_path / "phase.jsonl"
    script = ScenarioTransport(main, worker)
    attach(session, RecordingTransport(script, cassette))
    session._wf_config_base = replace(
        session._wf_config_base, agent_runner=session._ctx.agent_runner
    )
    select_generate(session)

    def status() -> str:
        return Text.from_markup(session._workspace.status.markup(160)).plain

    scroll = session._workspace.scroll
    scroll.mount()
    try:
        task = await send(session, "Delegate inside a workflow.")
        await until(lambda: "waiting for phase subagents" in status())
        assert "1 running" in status()
        assert not session._ctx.app_state.conversation.running_tools()
        assert "The spawn returned." in session._ctx.console.export_text(clear=False)
        session.handle_interrupt(InterruptAgentCommand(source="escape", disposition="pause"))
        await task
        assert "phase subagent delivery paused" in status()
        assert session.route("/workflow resume")
        task = session._agent_task
        assert task is not None
        (pool,) = source_for(session).registry.handles()
        await until(lambda: "waiting for phase subagents" in status())
        release.set()
        await pool.task
        session.reconcile_subagent_wakes()
        session.reconcile_subagent_wakes()
        await until(provider_resumed.is_set)
        assert "phase subagent results ready" in status()
        finish.set()
        async with asyncio.timeout(8):
            await task
        scroll.flush()
    finally:
        scroll.unmount()
    rendered = session._ctx.console.export_text(clear=False)
    assert rendered.count("↩ Subagent finished") == 1
    assert "The phase finished." in rendered
    assert [
        name
        for entry in SessionCassette.from_path(cassette).entries
        for name in entry.called_tool_names
    ] == [
        "spawn_subagents",
        "collect_subagent_results",
        "mark_execute_complete",
    ]
