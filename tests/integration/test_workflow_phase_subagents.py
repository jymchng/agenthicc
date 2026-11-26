"""Scripted providers at the real interactive workflow and tool boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from lauren_ai._transport import Completion, ToolCall
from rich.text import Text

from agenthicc.runners.tui_session import TUISession
from agenthicc.subagents.delivery import JournalNoticeSource
from agenthicc.tui.runtime.commands import InterruptAgentCommand, SendMessageCommand
from agenthicc.workflows.plugin import PhaseRole, PhaseSpec, WorkflowPlugin
from tests.e2e.self_extension.harness import ModelRequest, reply, tool_call
from tests.subagent_sessions import (
    WORKER_RESULT,
    ScenarioTransport,
    attach,
    detach_controller,
    hosted_controller,
    send,
    send_detached_input,
    spawn_call,
    tui,
    until,
)
from tests.subagent_sessions import session as session

pytestmark = pytest.mark.integration


@pytest.fixture(params=[False, True], ids=["attached", "detached"])
async def phase_runtime(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[TUISession, bool]]:
    detached = request.param is True
    async with tui(tmp_path, monkeypatch, host_controller=detached) as instance:
        yield instance, detached


class GenerateWorkflow(WorkflowPlugin):
    name = "phase_subagent_fixture"
    phases = [
        PhaseSpec("generate", agent_type=PhaseRole.EXECUTOR, require_explicit_completion=True)
    ]


def attach_phase(session: TUISession, script: ScenarioTransport) -> None:
    attach(session, script)
    session._wf_config_base = replace(
        session._wf_config_base, agent_runner=session._ctx.agent_runner
    )


def select_generate(session: TUISession) -> None:
    session._ctx.workflow_registry.register(GenerateWorkflow)
    assert session.route(f"/workflow {GenerateWorkflow.name}")


def source_for(session: TUISession) -> JournalNoticeSource:
    source = session._install_subagent_notices()
    assert source is not None
    return source


def pool_id(session: TUISession) -> str:
    (pool,) = source_for(session).registry.handles()
    return pool.pool_id


def transition(call_id: str = "transition") -> ToolCall:
    return ToolCall(tool_use_id=call_id, name="mark_execute_complete", input={"summary": "done"})


@pytest.mark.parametrize("transition_first", [True, False])
async def test_a1_a2_a3_generate_releases_batch_then_collects_before_transition(
    phase_runtime: tuple[TUISession, bool], tmp_path: Path, transition_first: bool
) -> None:
    session, detached = phase_runtime
    Path("source.txt").write_text("independent sibling read", encoding="utf-8")
    release_worker = asyncio.Event()
    worker_entered = asyncio.Event()
    parent_entered = asyncio.Event()
    release_parent = asyncio.Event()
    continue_parent = asyncio.Event()
    ownership: list[tuple[str, str, str]] = []

    async def worker(_: ModelRequest) -> Completion:
        worker_entered.set()
        await release_worker.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence in {1, 3}:
            handle = session._workflow_handle
            assert handle is not None
            ownership.append(
                (
                    handle.frame_id,
                    handle.phase_invocation,
                    session._ctx.app_state.active_mode().name,
                )
            )
        if sequence == 1:
            parent_entered.set()
            await release_parent.wait()
            spawn = spawn_call()
            calls = [*spawn.tool_calls, transition()]
            if transition_first:
                calls.reverse()
            calls.append(
                ToolCall(tool_use_id="sibling", name="read_file", input={"path": "source.txt"})
            )
            return replace(spawn, tool_calls=calls)
        if sequence == 2:
            assert request.contains('"status": "running"'), request.messages
            assert request.contains("independent sibling read")
            assert request.contains("queued workflow input")
            assert request.contains("subagents_pending")
            assert not release_worker.is_set() and not request.contains(WORKER_RESULT)
            continue_parent.set()
            return reply("Waiting for the workers.")
        if sequence == 3:
            assert request.contains(f"Background subagent pool {pool_id(session)} completed")
            assert not request.contains(WORKER_RESULT)
            collect = tool_call(
                "collect_subagent_results", {"pool_id": pool_id(session)}, use_id="collect"
            )
            calls = [*collect.tool_calls, transition("too-early")]
            if transition_first:
                calls.reverse()
            return replace(collect, tool_calls=calls)
        if sequence == 4:
            assert request.contains(WORKER_RESULT)
            journal = source_for(session).journal
            handle = session._workflow_handle
            assert handle is not None and handle.current_phase == "generate"
            assert journal.collected_phase_pools(handle.phase_invocation) == {pool_id(session)}
            assert repr(request.messages).count("subagents_pending") == 2
            return tool_call("mark_execute_complete", {"summary": "done"}, use_id="allowed")
        assert sequence == 5
        return reply("Generate complete.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    select_generate(session)
    task = await send(session, "Generate from the fixture.")
    await until(parent_entered.is_set)
    await session.handle_send(SendMessageCommand(text="queued workflow input"))
    release_parent.set()
    await until(lambda: continue_parent.is_set() or task.done())
    assert not task.done()
    await until(worker_entered.is_set)
    handle = session._workflow_handle
    assert handle is not None and handle.current_phase == "generate"
    journal = source_for(session).journal
    (record,) = journal.fold_subagent_pool_lifecycle()
    assert record["backgrounded"] is True
    assert record["phase_owner"] == {
        "conversation_id": session._ctx.session_id,
        "run_id": handle.run_id,
        "frame_id": handle.frame_id,
        "phase": "generate",
        "invocation": handle.phase_invocation,
    }
    conv = session._ctx.app_state.conversation
    await until(lambda: conv.phase_subagent_wait() == "waiting for phase subagents")
    assert not conv.running_tools()
    assert (
        "waiting for phase subagents"
        in Text.from_markup(session._workspace.status.markup(160)).plain
    )
    phase_progress = (handle.phase_iteration, handle.checkpoint_revision)
    await asyncio.sleep(0.3)
    assert len(script.main_requests) == 2
    assert (handle.phase_iteration, handle.checkpoint_revision) == phase_progress
    assert handle.current_phase == "generate"
    if detached:
        await detach_controller(session, tmp_path)
        assert (handle.phase_iteration, handle.checkpoint_revision) == phase_progress
    release_worker.set()
    async with asyncio.timeout(8):
        await task
    assert len(script.main_requests) == 5
    assert script.main_tool_schemas[0] == script.main_tool_schemas[2]
    assert ownership[0] == ownership[1]
    assert handle.lifecycle == "complete"


async def test_a5_a6_pause_defers_terminal_wake_and_resumes_same_phase(
    phase_runtime: tuple[TUISession, bool], tmp_path: Path
) -> None:
    session, detached = phase_runtime
    release = asyncio.Event()

    async def worker(_: ModelRequest) -> Completion:
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            assert request.contains('"status": "running"'), request.messages
            return reply("Wait here.")
        if sequence == 3:
            assert request.contains(f"Background subagent pool {pool_id(session)} completed")
            return tool_call(
                "collect_subagent_results", {"pool_id": pool_id(session)}, use_id="collected"
            )
        if sequence == 4:
            assert request.contains(WORKER_RESULT)
            assert request.contains("first queued phase input")
            assert not request.contains("second queued phase input")
            return tool_call("mark_execute_complete", {"summary": "done"}, use_id="finish")
        assert sequence == 5
        history = repr(request.messages)
        assert (
            history.count("first queued phase input")
            == history.count("second queued phase input")
            == 1
        )
        assert history.index("first queued phase input") < history.index(
            "second queued phase input"
        )
        return reply("Done.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    select_generate(session)
    task = await send(session, "Generate then wait.")
    conv = session._ctx.app_state.conversation
    await until(lambda: bool(conv.phase_subagent_wait()) or task.done())
    assert not task.done()
    handle = session._workflow_handle
    assert handle is not None
    invocation = handle.phase_invocation
    session.handle_interrupt(InterruptAgentCommand(source="escape", disposition="pause"))
    await session.handle_send(SendMessageCommand(text="first queued phase input"))
    await task
    assert handle.lifecycle == "paused"
    await session.handle_send(SendMessageCommand(text="second queued phase input"))
    assert session._agent_task is None
    (pool,) = source_for(session).registry.handles()
    assert not pool.task.done()
    if detached:
        await detach_controller(session, tmp_path)
        assert handle.lifecycle == "paused"
    release.set()
    await pool.task
    assert len(script.main_requests) == 2
    assert not source_for(session).has_pending()
    if detached:
        send_detached_input(session, tmp_path, "/workflow resume")
        await until(lambda: session._agent_task is not None)
    else:
        assert session.route("/workflow resume")
    resumed_task = session._agent_task
    assert resumed_task is not None
    async with asyncio.timeout(8):
        await resumed_task
    assert handle.phase_invocation == invocation
    assert handle.lifecycle == "complete"
    assert len(script.main_requests) == 5
    assert source_for(session).journal.delivered_subagent_pool_notifications() == {pool.pool_id}


async def test_a4_a7_operator_foreground_waits_in_shared_default_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with tui(
        tmp_path, monkeypatch, config_text='[subagents]\nspawn_mode="foreground"\n'
    ) as session:
        release = asyncio.Event()
        started = asyncio.Event()

        async def worker(_: ModelRequest) -> Completion:
            started.set()
            await release.wait()
            return reply(WORKER_RESULT)

        async def main(request: ModelRequest, sequence: int) -> Completion:
            if sequence == 1:
                assert "does not block you" not in (request.system or "")
                return spawn_call()
            if sequence == 2:
                assert request.contains(WORKER_RESULT)
                return tool_call("mark_execute_complete", {"summary": "done"}, use_id="finish")
            assert sequence == 3
            return reply("Done.")

        script = ScenarioTransport(main, worker)
        attach_phase(session, script)
        select_generate(session)
        task = await send(session, "Wait for the worker.")
        await until(lambda: started.is_set() or task.done())
        assert not task.done() and len(script.main_requests) == 1
        release.set()
        async with asyncio.timeout(8):
            await task
        assert len(script.main_requests) == 3


@pytest.mark.parametrize("manual_detach", [False, True])
@pytest.mark.parametrize("terminal_handoff", [False, True])
async def test_a1_a3_a4_a6_make_book_research_collects_then_validates_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual_detach: bool, terminal_handoff: bool
) -> None:
    import json
    import re

    from agenthicc.tui.runtime.commands import BackgroundSubagentsCommand
    from agenthicc.workflows.make_book.runner import MakeBookContext

    setting = "foreground" if manual_detach else "background"
    async with tui(
        tmp_path,
        monkeypatch,
        config_text=f'[subagents]\nspawn_mode="{setting}"\n',
        host_controller=terminal_handoff,
    ) as session:
        registry = session._ctx.subagent_registry
        researcher = registry.get("researcher")
        assert researcher is not None
        registry.register(replace(researcher, allowed_tools=frozenset({"read_file"})))
        output = Path("book").resolve()
        research = output / "research"
        research.mkdir(parents=True)
        (research / "sources.md").write_text("Independent research sources", encoding="utf-8")
        started = asyncio.Event()
        release = asyncio.Event()
        in_research = asyncio.Event()
        continue_parent = asyncio.Event()
        assets = asyncio.Event()

        async def worker(request: ModelRequest) -> Completion:
            assert "read_file" in request.tools and "write_file" not in request.tools
            started.set()
            await release.wait()
            return reply(WORKER_RESULT)

        async def main(request: ModelRequest, sequence: int) -> Completion:
            if sequence == 1:
                match = re.search(
                    r"Write a small JSON object to (.+?) with title", str(request.system)
                )
                if match is None:
                    match = re.search(
                        r"Write a small JSON object to (.+?) with title", str(request.messages)
                    )
                assert match is not None
                manifest = Path(match.group(1))
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text(
                    json.dumps(
                        {
                            "title": "Phase recovery fixture",
                            "chapters": [{"title": "One", "outline": "First chapter"}],
                            "output_dir": str(output),
                        }
                    ),
                    encoding="utf-8",
                )
                return tool_call("submit_toc", {"summary": "TOC is ready."})
            if sequence == 2:
                assert request.contains("TOC")
                return reply("TOC complete.")
            if sequence == 3:
                assert "submit_research" in request.tools
                in_research.set()
                await continue_parent.wait()
                spawn = spawn_call(kind="researcher")
                return replace(
                    spawn,
                    tool_calls=[
                        *spawn.tool_calls,
                        ToolCall(
                            tool_use_id="read-sources",
                            name="read_file",
                            input={
                                "path": str(research / "sources.md"),
                            },
                        ),
                    ],
                )
            if sequence == 4:
                expected = "backgrounded" if manual_detach else "running"
                assert request.contains(f'"status": "{expected}"')
                assert request.contains("Independent research sources")
                assert request.contains("Include my queued research requirement.")
                assert not release.is_set() and not request.contains(WORKER_RESULT)
                return reply("Preserve the Research phase while workers finish.")
            if sequence == 5:
                assert request.contains(f"Background subagent pool {pool_id(session)} completed")
                return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
            if sequence == 6:
                assert request.contains(WORKER_RESULT)
                return tool_call(
                    "submit_research", {"summary": "Missing chapter notes"}, use_id="missing"
                )
            if sequence == 7:
                assert request.contains("research handoff is missing chapter note files")
                handle = session._workflow_handle
                assert handle is not None and handle.current_phase == "research"
                assert isinstance(handle.context, MakeBookContext)
                assert [receipt["phase"] for receipt in handle.context.transition_receipts] == [
                    "toc"
                ]
                (research / "ch01-notes.md").write_text(WORKER_RESULT, encoding="utf-8")
                return tool_call(
                    "submit_research", {"summary": "Repaired chapter coverage"}, use_id="fixed"
                )
            if sequence == 8:
                return reply("Research complete.")
            assert sequence == 9 and "confirm_assets_ready" in request.tools
            assets.set()
            await asyncio.Event().wait()
            return reply("Unreachable.")

        script = ScenarioTransport(main, worker)
        attach_phase(session, script)
        assert session.route("/workflow make_book")
        task = await send(session, "Write a short technical book.")
        await until(lambda: in_research.is_set() or task.done())
        assert not task.done()
        await session.handle_send(
            SendMessageCommand(text="Include my queued research requirement.")
        )
        continue_parent.set()
        await until(lambda: started.is_set() or task.done())
        assert not task.done()
        if manual_detach:
            conv = session._ctx.app_state.conversation
            assert len(script.main_requests) == 3 and conv.subagent_foreground()
            await session._background_subagents_command(BackgroundSubagentsCommand())
        conv = session._ctx.app_state.conversation
        await until(lambda: bool(conv.phase_subagent_wait()) or task.done())
        assert not task.done() and len(script.main_requests) == 4
        state = conv.subagent_pool_state()
        assert state is not None and state.active
        if terminal_handoff:
            await detach_controller(session, tmp_path)
            assert state == conv.subagent_pool_state()
            assert len(script.main_requests) == 4
        release.set()
        await until(lambda: assets.is_set() or task.done())
        assert assets.is_set()
        handle = session._workflow_handle
        assert handle is not None and handle.current_phase == "assets"
        assert isinstance(handle.context, MakeBookContext)
        assert [receipt["phase"] for receipt in handle.context.transition_receipts] == [
            "toc",
            "research",
        ]
        assert script.main_requests[2].tools == script.main_requests[4].tools
        session.handle_interrupt(InterruptAgentCommand(source="escape", disposition="pause"))
        await task


@pytest.mark.parametrize("lost_hint", [False, True])
async def test_a2_question_is_answered_and_same_phase_collects_even_if_hint_is_lost(
    phase_runtime: tuple[TUISession, bool],
    tmp_path: Path,
    lost_hint: bool,
) -> None:
    session, detached = phase_runtime
    ask = asyncio.Event()
    question = "Which heading should be used?"

    async def worker(request: ModelRequest) -> Completion:
        if request.contains("use the supplied heading"):
            return reply(WORKER_RESULT)
        if request.contains("tool_result"):
            return reply("Waiting for the answer.")
        await ask.wait()
        return tool_call("ask_parent", {"question": question, "timeout_s": 15}, use_id="ask")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        pool = pool_id(session)
        if sequence == 2:
            return reply("Await the question.")
        if sequence == 3:
            assert request.contains(f"background subagent pool {pool} is waiting")
            assert not request.contains(question)
            return tool_call("collect_subagent_results", {"pool_id": pool}, use_id="read-question")
        if sequence == 4:
            assert request.contains(question)
            handle = source_for(session).registry.get(pool)
            assert handle is not None
            (pending,) = handle.broker.pending_questions(recipient="main")
            return tool_call(
                "answer_subagent",
                {
                    "pool_id": pool,
                    "question_id": str(pending["question_id"]),
                    "answer": "use the supplied heading",
                },
            )
        if sequence == 5 and not request.contains(f"Background subagent pool {pool} completed"):
            return reply("The heading was answered.")
        if not request.contains(WORKER_RESULT):
            assert request.contains(f"Background subagent pool {pool} completed")
            return tool_call("collect_subagent_results", {"pool_id": pool}, use_id="terminal")
        if not request.contains("Implementation marked complete"):
            return tool_call("mark_execute_complete", {"summary": "done"})
        return reply("Complete.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    select_generate(session)
    source = source_for(session)
    if lost_hint:
        source.registry.on_terminal(None)
        source.registry.on_activity(None)
    task = await send(session, "Ask a worker to inspect the heading.")
    conv = session._ctx.app_state.conversation
    await until(lambda: bool(conv.phase_subagent_wait()) or task.done())
    assert not task.done()
    handle = session._workflow_handle
    assert handle is not None
    if detached:
        await detach_controller(session, tmp_path)
        assert len(script.main_requests) == 2
    ask.set()
    async with asyncio.timeout(8):
        await task
    assert len(script.worker_requests) == 3
    assert handle.lifecycle == "complete"


@pytest.mark.parametrize("implicit_next", [None, "finish"])
async def test_a3_implicit_phase_edges_wait_for_all_pools(
    phase_runtime: tuple[TUISession, bool], tmp_path: Path, implicit_next: str | None
) -> None:
    session, detached = phase_runtime

    class Implicit(WorkflowPlugin):
        name = "implicit_phase_fixture"
        phases = [PhaseSpec("generate", next=implicit_next), PhaseSpec("finish")]

    release_one, release_two = asyncio.Event(), asyncio.Event()
    tried_limit = asyncio.Event()
    one_collected = asyncio.Event()

    async def worker(request: ModelRequest) -> Completion:
        if request.contains("first pool"):
            await release_one.wait()
            return reply("First worker evidence.")
        assert request.contains("second pool")
        await release_two.wait()
        return reply("Second worker evidence.")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence <= 3:
            if sequence == 3:
                assert request.contains('"status": "running"')
            label = ("first pool", "second pool", "refused pool")[sequence - 1]
            spawn = spawn_call(task=label)
            (call,) = spawn.tool_calls
            return replace(spawn, tool_calls=[replace(call, tool_use_id=f"pool-{sequence}")])
        records = source_for(session).journal.fold_subagent_pool_lifecycle()
        assert len(records) == 2
        first, second = [str(record["pool_id"]) for record in records]
        if sequence == 4:
            assert request.contains("Active subagent pool limit")
            tried_limit.set()
            return reply("I yielded while both pools run.")
        if sequence == 5:
            assert request.contains(f"Background subagent pool {first} completed")
            live = tool_call("collect_subagent_results", {"pool_id": second}, use_id="live")
            terminal = tool_call("collect_subagent_results", {"pool_id": first}, use_id="first")
            return replace(live, tool_calls=[*live.tool_calls, *terminal.tool_calls])
        if sequence == 6:
            assert request.contains("First worker evidence.")
            assert not request.contains("Second worker evidence.")
            handle = session._workflow_handle
            assert handle is not None and handle.current_phase == "generate"
            assert source_for(session).journal.collected_phase_pools(handle.phase_invocation) == {
                first
            }
            one_collected.set()
            return reply("I yielded while the second pool runs.")
        if sequence == 7:
            assert request.contains(f"Background subagent pool {second} completed")
            return tool_call("collect_subagent_results", {"pool_id": second}, use_id="second")
        if sequence == 8:
            assert request.contains("Second worker evidence.")
            return reply("Both results are collected.")
        assert implicit_next == "finish" and sequence == 9
        assert session._workflow_handle is not None
        assert session._workflow_handle.current_phase == "finish"
        return reply("Finished after Generate.")

    script = ScenarioTransport(main, worker)
    cfg = session._ctx.cfg
    session._wf_config_base = replace(
        session._wf_config_base,
        cfg=replace(cfg, subagents=replace(cfg.subagents, max_active_pools=2)),
    )
    attach_phase(session, script)
    session._ctx.workflow_registry.register(Implicit)
    assert session.route(f"/workflow {Implicit.name}")
    task = await send(session, "Run a phase with an implicit edge.")
    await until(lambda: tried_limit.is_set() or task.done())
    assert not task.done()
    handle = session._workflow_handle
    assert handle is not None
    if detached:
        await detach_controller(session, tmp_path)
        assert (
            len(
                [
                    handle
                    for handle in source_for(session).registry.handles()
                    if not handle.task.done()
                ]
            )
            == 2
        )
    release_one.set()
    await until(lambda: one_collected.is_set() or task.done())
    assert not task.done()
    release_two.set()
    async with asyncio.timeout(8):
        await task
    assert handle.lifecycle == "complete"
    assert len(script.worker_requests) == 2


async def test_a2_busy_phase_delivers_committed_read_before_notice_and_input(
    session: TUISession,
) -> None:
    Path("sibling.txt").write_text("Active phase sibling evidence", encoding="utf-8")
    release_worker, parent_in_provider, continue_parent = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def worker(_: ModelRequest) -> Completion:
        await release_worker.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            parent_in_provider.set()
            await continue_parent.wait()
            return tool_call("read_file", {"path": "sibling.txt"}, use_id="sibling-active")
        if sequence == 3:
            ordered = repr(request.messages)
            notice = f"Background subagent pool {pool_id(session)} completed"
            assert ordered.index("Active phase sibling evidence") < ordered.index(notice)
            assert ordered.index(notice) < ordered.index("Use my queued update.")
            assert ordered.count("Use my queued update.") == 1 and WORKER_RESULT not in ordered
            return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
        if sequence == 4:
            return tool_call("mark_execute_complete", {"summary": "Collected and complete."})
        assert sequence == 5
        return reply("Complete.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    select_generate(session)
    task = await send(session, "Start a pool while I keep working.")
    await until(lambda: parent_in_provider.is_set() or task.done())
    assert not task.done()
    (pool,) = source_for(session).registry.handles()
    release_worker.set()
    await pool.task
    await session.handle_send(SendMessageCommand(text="Use my queued update."))
    continue_parent.set()
    async with asyncio.timeout(8):
        await task
    assert len(script.main_requests) == 5


@pytest.mark.parametrize("terminal_handoff", [False, True])
@pytest.mark.parametrize("exit_action", ["reset", "abandon", "timeout"])
async def test_a5_phase_exit_settles_pool_or_preserves_ownership_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_action: str, terminal_handoff: bool
) -> None:
    config = "[execution]\nturn_timeout_s = 5.0\n" if exit_action == "timeout" else ""
    async with tui(
        tmp_path, monkeypatch, config_text=config, host_controller=terminal_handoff
    ) as session:
        blocked = asyncio.Event()

        async def worker(_: ModelRequest) -> Completion:
            await blocked.wait()
            return reply(WORKER_RESULT)

        async def main(_: ModelRequest, sequence: int) -> Completion:
            if sequence == 1:
                return spawn_call()
            assert sequence == 2
            return reply("Waiting on a worker.")

        script = ScenarioTransport(main, worker)
        attach_phase(session, script)
        if exit_action == "abandon":
            session._ctx.workflow_registry.register(GenerateWorkflow)
            await session.handle_send(
                SendMessageCommand(text=f"/workflow call {GenerateWorkflow.name} Wait with child.")
            )
            task = session._agent_task
            assert task is not None
        else:
            select_generate(session)
            task = await send(session, "Wait for a worker.")
        conv = session._ctx.app_state.conversation
        await until(lambda: bool(conv.phase_subagent_wait()) or task.done())
        assert not task.done()
        handle = session._workflow_handle
        assert handle is not None
        source = source_for(session)
        (pool,) = source.registry.handles()
        if terminal_handoff:
            await detach_controller(session, tmp_path)
        else:
            assert not session._request_background_handoff()
            assert conv.notification() is not None and "Collect" in conv.notification()
        if exit_action != "timeout":
            session.handle_interrupt(InterruptAgentCommand(source="escape", disposition="pause"))
        async with asyncio.timeout(8):
            await task
        assert handle.lifecycle == "paused" and not pool.task.done()
        assert conv.phase_subagent_wait() == "phase subagent delivery paused"
        if exit_action == "timeout":
            assert handle.failure_kind == "timeout"
            assert (
                pool.phase_owner is not None
                and pool.phase_owner.invocation == handle.phase_invocation
            )
            return
        command = "/workflow return --abandon" if exit_action == "abandon" else "/workflow reset"
        if terminal_handoff:
            send_detached_input(session, tmp_path, command)
        else:
            assert session.route(command)
        await until(lambda: handle.lifecycle == "discarded")
        assert pool.task.cancelled()
        (record,) = source.journal.fold_subagent_pool_lifecycle()
        assert record["status"] == "cancelled"
        assert session._ctx.workflow_stack.depth == 0


@pytest.mark.parametrize("decision", ["approve_review", "reject_review"])
async def test_a3_a4_review_transition_and_reentry_preserve_invocation(
    phase_runtime: tuple[TUISession, bool], tmp_path: Path, decision: str
) -> None:
    session, detached = phase_runtime

    class Reviewed(WorkflowPlugin):
        name = "review_phase_fixture"
        phases = [
            PhaseSpec("generate", next="review", max_iterations=2),
            PhaseSpec("review", on_reject="generate", require_explicit_review=True),
        ]

    release_review, release_generate = asyncio.Event(), asyncio.Event()
    generate_invocations: list[str] = []
    review_pool = ""
    generate_pool = ""
    rejected = False

    async def worker(request: ModelRequest) -> Completion:
        release = release_review if request.contains("review job") else release_generate
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        nonlocal review_pool, generate_pool, rejected
        handle = session._workflow_handle
        assert handle is not None
        review_input = {"summary": "OK"} if decision == "approve_review" else {"reason": "Redo"}
        if sequence == 1:
            generate_invocations.append(handle.phase_invocation)
            return reply("Initial Generate.")
        if sequence == 2:
            assert handle.current_phase == "review"
            return spawn_call(task="review job")
        if sequence == 3:
            review_pool = pool_id(session)
            return tool_call(decision, review_input, use_id="early-review")
        if sequence == 4:
            assert request.contains("subagents_pending")
            rejected = True
            return reply("Review will wait.")
        if sequence == 5:
            assert request.contains(f"Background subagent pool {review_pool} completed")
            return tool_call(
                "collect_subagent_results", {"pool_id": review_pool}, use_id="review-collect"
            )
        if sequence == 6:
            return tool_call(decision, review_input, use_id="accepted-review")
        if sequence == 7:
            return reply("Review recorded.")
        if sequence >= 8 and handle.current_phase == "review":
            assert decision == "reject_review" and handle.phase_iteration == 2
            if not request.contains("review after reentry"):
                return tool_call(
                    "approve_review", {"summary": "review after reentry"}, use_id="approval-final"
                )
            return reply("Review complete.")
        if sequence == 8:
            assert decision == "reject_review" and handle.current_phase == "generate"
            generate_invocations.append(handle.phase_invocation)
            spawn = spawn_call(task="redo job")
            (call,) = spawn.tool_calls
            return replace(spawn, tool_calls=[replace(call, tool_use_id="generate-spawn")])
        if sequence == 9:
            ids = [pool.pool_id for pool in source_for(session).registry.handles()]
            (generate_pool,) = [identity for identity in ids if identity != review_pool]
            return tool_call("collect_subagent_results", {"pool_id": review_pool}, use_id="foreign")
        if sequence == 10:
            assert request.contains("unknown subagent pool")
            return reply("Pause before the regenerated findings arrive.")
        if sequence == 11:
            assert handle.phase_invocation == generate_invocations[1]
            assert request.contains(f"Background subagent pool {generate_pool} completed")
            return tool_call(
                "collect_subagent_results", {"pool_id": generate_pool}, use_id="redo-collect"
            )
        assert sequence == 12
        return reply("Regenerated and collected.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    session._ctx.workflow_registry.register(Reviewed)
    assert session.route(f"/workflow {Reviewed.name}")
    task = await send(session, "Generate, then review.")
    await until(lambda: rejected or task.done())
    assert not task.done()
    handle = session._workflow_handle
    assert handle is not None and handle.current_phase == "review"
    if detached:
        await detach_controller(session, tmp_path)
        assert session._workflow_handle is handle
    release_review.set()
    if decision == "approve_review":
        async with asyncio.timeout(8):
            await task
        assert len(script.main_requests) == 7 and handle.lifecycle == "complete"
        return
    conv = session._ctx.app_state.conversation
    await until(
        lambda: (
            task.done() or (len(script.main_requests) == 10 and bool(conv.phase_subagent_wait()))
        )
    )
    assert not task.done() and len(set(generate_invocations)) == 2
    session.handle_interrupt(InterruptAgentCommand(source="escape", disposition="pause"))
    await task
    assert handle.phase_iteration == 2 and handle.lifecycle == "paused"
    release_generate.set()
    pool = source_for(session).registry.get(generate_pool)
    assert pool is not None
    await pool.task
    if detached:
        send_detached_input(session, tmp_path, "/workflow resume")
        await until(lambda: session._agent_task is not None)
    else:
        assert session.route("/workflow resume")
    resumed = session._agent_task
    assert resumed is not None
    async with asyncio.timeout(8):
        await resumed
    assert handle.lifecycle == "complete"
    assert len(script.worker_requests) == 2


@pytest.mark.parametrize(
    "seam", ["live", "terminal", "message", "ack", "before-commit", "collected"]
)
async def test_a5_reopen_phase_with_durable_pools_without_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    import json
    import sys

    from agenthicc.subagents.phase import PhaseOwner

    entered = asyncio.Event()

    async def no_worker(_: ModelRequest) -> Completion:
        pytest.fail("Recovery must never relaunch a worker")

    async def initial(_: ModelRequest, __: int) -> Completion:
        entered.set()
        await asyncio.Event().wait()
        return reply("Unreachable.")

    async with tui(tmp_path, monkeypatch) as session:
        attach_phase(session, ScenarioTransport(initial, no_worker))
        select_generate(session)
        task = await send(session, "Start a durable phase.")
        await until(entered.is_set)
        handle = session._workflow_handle
        assert handle is not None
        identity = PhaseOwner(
            session._ctx.session_id,
            handle.run_id,
            handle.frame_id,
            "generate",
            handle.phase_invocation,
        )
        journal_path = source_for(session).journal.path
        session.handle_interrupt(InterruptAgentCommand(source="escape", disposition="pause"))
        await task
        assert handle.lifecycle == "paused"
        handle.release_claim()

    # The child writes exactly the durable prefix that survives each crash seam.
    code = """
import json
import os
import sys
from pathlib import Path
from agenthicc.memory.journal import ConversationJournal
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.subagents.delivery import background_pool_notice
from agenthicc.subagents.phase import PhaseOwner, PhasePools, bind_phase_pools
from lauren_ai._tools import ToolResult
from lauren_ai._transport import Completion, TokenUsage, ToolCall

path, seam, raw_scope = sys.argv[1:]
owner = json.loads(raw_scope)
journal = ConversationJournal(Path(path))
journal.subagent_pool_started(
    pool_id="restart-pool", fingerprint="fingerprint", parent_run_id="turn",
    tasks=[{"task_id": "task-0", "agent_type": "explorer"}], phase_owner=owner,
)
journal.subagent_pool_backgrounded(pool_id="restart-pool", tool_result={"status": "running"})
if seam != "live":
    journal.subagent_pool_finished(
        pool_id="restart-pool", total=1, succeeded=1, failed=0, text="terminal evidence",
    )
if seam in {"message", "ack", "before-commit", "collected"}:
    record, = journal.completed_background_subagent_pools()
    memory = JournaledShortTermMemory(journal)
    memory.add_user_once(
        background_pool_notice(record), message_id="subagent-completion:restart-pool",
    )
if seam in {"ack", "before-commit", "collected"}:
    journal.subagent_pool_notification_delivered(
        pool_id="restart-pool", message_id="subagent-completion:restart-pool",
    )
if seam in {"before-commit", "collected"}:
    class CrashMemory(JournaledShortTermMemory):
        def on_tool_exchange_committed(self, exchange):
            if seam == "collected":
                super().on_tool_exchange_committed(exchange)

    memory = CrashMemory(journal)
    call = ToolCall(
        tool_use_id="collection-before-crash", name="collect_subagent_results",
        input={"pool_id": "restart-pool"},
    )
    memory.add_assistant(Completion(
        id="collect", model="offline", content="", tool_calls=[call],
        stop_reason="tool_use", usage=TokenUsage(input_tokens=1, output_tokens=1),
    ))
    with bind_phase_pools(PhasePools(PhaseOwner(**owner), journal, frozenset(), available=True)):
        exchange = memory.begin_tool_exchange([call], run_id="turn-before-crash")
        memory.commit_tool_exchange(exchange, [
            ToolResult.ok(json.dumps({
                "pool_id": "restart-pool", "status": "completed", "results": "terminal evidence",
            }), tool_use_id=call.tool_use_id),
        ])
print("ready", flush=True)
sys.stdin.readline()
os._exit(0)
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(journal_path),
        seam,
        json.dumps(identity.record()),
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        assert await asyncio.wait_for(proc.stdout.readline(), timeout=8) == b"ready\n"
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()

    async with tui(tmp_path, monkeypatch, resume_id=identity.conversation_id) as resumed:

        async def main(request: ModelRequest, sequence: int) -> Completion:
            handle = resumed._workflow_handle
            assert handle is not None
            assert (
                handle.phase_invocation == identity.invocation
                and handle.frame_id == identity.frame_id
            )
            assert repr(request.messages).count("Background subagent pool restart-pool") == 1
            if sequence == 1 and seam in {"before-commit", "collected"}:
                assert request.contains("terminal evidence")
            if sequence == 1 and seam != "collected":
                return tool_call("mark_execute_complete", {"summary": "Too early"}, use_id="early")
            if sequence == 2 and seam != "collected":
                assert request.contains("subagents_pending")
                return tool_call("collect_subagent_results", {"pool_id": "restart-pool"})
            if sequence == (1 if seam == "collected" else 3):
                if seam == "live":
                    assert request.contains("interrupted by process restart")
                elif seam != "collected":
                    assert request.contains("terminal evidence")
                return tool_call("mark_execute_complete", {"summary": "Collected"}, use_id="done")
            return reply("Recovery complete.")

        script = ScenarioTransport(main, no_worker)
        attach_phase(resumed, script)
        resumed._ctx.workflow_registry.register(GenerateWorkflow)
        resumed._restore_paused_workflow()
        source = source_for(resumed)
        resumed.reconcile_subagent_wakes()
        assert resumed._agent_task is None and not script.main_requests and not source.has_pending()
        assert resumed.route("/workflow resume")
        task = resumed._agent_task
        assert task is not None, resumed._ctx.app_state.conversation.notification()
        async with asyncio.timeout(8):
            await task
        assert len(script.main_requests) == (2 if seam == "collected" else 4)
        assert not script.worker_requests and not source.registry.handles()
        assert source.journal.collected_phase_pools(identity.invocation) == {"restart-pool"}
        assert source.journal.delivered_subagent_pool_notifications() == {"restart-pool"}


async def test_a3_a5_admitted_execution_failure_is_terminal_and_collectable(
    session: TUISession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.subagents.pool import SubagentPool

    async def fail_after_admission(pool: SubagentPool, _ready: object) -> None:
        raise RuntimeError("scripted settlement failure")

    monkeypatch.setattr(SubagentPool, "_run_admitted", fail_after_admission)

    async def no_worker(_: ModelRequest) -> Completion:
        pytest.fail("Worker must never start after the scripted failure")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            (record,) = source_for(session).journal.fold_subagent_pool_lifecycle()
            assert record["status"] == "completed" and record["failed"] == 1
            assert request.contains("0/1 succeeded, 1 failed")
            return tool_call("collect_subagent_results", {"pool_id": str(record["pool_id"])})
        if sequence == 3:
            assert request.contains("scripted settlement failure")
            return tool_call("mark_execute_complete", {"summary": "Handled failure"})
        assert sequence == 4
        return reply("Complete.")

    script = ScenarioTransport(main, no_worker)
    attach_phase(session, script)
    select_generate(session)
    task = await send(session, "Delegate then handle a failure.")
    async with asyncio.timeout(8):
        await task
    assert len(script.main_requests) == 4 and not script.worker_requests


@pytest.mark.parametrize("restriction", ["collect", "answer", "memory", "parallel"])
async def test_a4_unsupported_phase_routes_keep_foreground_without_detach(
    session: TUISession,
    restriction: str,
) -> None:
    from lauren_ai._memory import ShortTermMemory

    from agenthicc.runners.agent_turn_context import AgentTurnOptions
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.default.runner import WorkflowRunner

    class RestrictedRunner(WorkflowRunner):
        async def _run_phase_turn(
            self,
            spec: PhaseSpec,
            text: str,
            options: AgentTurnOptions,
        ) -> None:
            scoped = options.copy()
            if restriction == "memory":
                scoped["session_memory"] = ShortTermMemory()
            elif restriction != "parallel":
                tools = {"spawn_subagents", "read_file"}
                if restriction != "collect":
                    tools.add("collect_subagent_results")
                if restriction != "answer":
                    tools.add("answer_subagent")
                scoped["allowed_tool_names"] = frozenset(tools)
            await super()._run_phase_turn(spec, text, scoped)

    class Restricted(WorkflowPlugin):
        name = "restricted_phase_fixture"
        phases = [
            PhaseSpec("generate", parallel_with=("peer",) if restriction == "parallel" else ()),
            PhaseSpec("peer"),
        ]

        @classmethod
        def build_runner(
            cls, config: WorkflowConfig, mode_manager: ModeManager | None
        ) -> WorkflowRunner:
            return RestrictedRunner(cls, config, mode_manager)

    entered, release = asyncio.Event(), asyncio.Event()

    async def worker(_: ModelRequest) -> Completion:
        entered.set()
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, _: int) -> Completion:
        if restriction == "parallel" and request.contains("peer-only marker"):
            return reply("Peer is done.")
        if not request.contains(WORKER_RESULT):
            if restriction in {"answer", "collect"}:
                assert (
                    "answer_subagent" not in request.tools
                    if restriction == "answer"
                    else "collect_subagent_results" not in request.tools
                )
            assert not request.contains("The phase waits here when you yield")
            return spawn_call()
        assert request.contains(WORKER_RESULT)
        return reply("Foreground result received.")

    if restriction == "parallel":
        Restricted.phases[1] = replace(
            Restricted.phases[1], system_prompt_override="peer-only marker"
        )
    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    session._ctx.workflow_registry.register(Restricted)
    assert session.route(f"/workflow {Restricted.name}")
    task = await send(session, "Inspect the fixture in a restricted phase.")
    await until(lambda: entered.is_set() or task.done())
    assert not task.done()
    (pool,) = source_for(session).registry.handles()
    assert pool.phase_owner is None and pool.pool is not None
    assert not pool.pool.detach_available
    assert pool.pool.state.active
    release.set()
    async with asyncio.timeout(8):
        await task
    assert len(script.worker_requests) == 1


async def test_a3_a4_nested_frame_returns_only_after_its_pool_is_collected(
    phase_runtime: tuple[TUISession, bool], tmp_path: Path
) -> None:
    session, detached = phase_runtime
    release = asyncio.Event()
    child_yielded = asyncio.Event()
    identity: list[tuple[str, str]] = []

    async def worker(_: ModelRequest) -> Completion:
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence <= 5:
            handle = session._workflow_handle
            assert handle is not None
            identity.append((handle.frame_id, handle.phase_invocation))
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            child_yielded.set()
            return reply("Child is waiting.")
        if sequence == 3:
            assert request.contains(f"Background subagent pool {pool_id(session)} completed")
            return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
        if sequence == 4:
            assert request.contains(WORKER_RESULT)
            return tool_call("mark_execute_complete", {"summary": "Collected"})
        if sequence == 5:
            return reply("Child is ready to return.")
        assert sequence == 6 and session._ctx.workflow_stack.depth == 0
        assert request.contains(GenerateWorkflow.name)
        return reply("Child result received.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    session._ctx.workflow_registry.register(GenerateWorkflow)
    await session.handle_send(
        SendMessageCommand(text=f"/workflow call {GenerateWorkflow.name} Start nested Generate.")
    )
    task = session._agent_task
    assert task is not None
    await until(lambda: child_yielded.is_set() or task.done())
    assert not task.done() and session._ctx.workflow_stack.depth == 1
    if detached:
        await detach_controller(session, tmp_path)
        assert session._ctx.workflow_stack.depth == 1
    release.set()
    async with asyncio.timeout(8):
        await task
    await until(lambda: len(script.main_requests) >= 6 and session._agent_task is None)
    assert len(set(identity)) == 1
    (record,) = source_for(session).journal.fold_subagent_pool_lifecycle()
    assert record["phase_owner"]["frame_id"] == identity[0][0]
    assert source_for(session).journal.collected_phase_pools(identity[0][1]) == {pool_id(session)}


@pytest.mark.parametrize("terminal_handoff", [False, True])
@pytest.mark.parametrize("governance", ["deadline", "capability-denied", "capability-allowed"])
async def test_a7_owned_workers_obey_deadline_and_mode_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    governance: str,
    terminal_handoff: bool,
) -> None:
    async with tui(
        tmp_path,
        monkeypatch,
        mode="Plan" if governance == "capability-denied" else "Yolo",
        host_controller=terminal_handoff,
    ) as session:
        release = asyncio.Event()
        can_write = governance == "capability-allowed"

        async def worker(request: ModelRequest) -> Completion:
            if governance == "deadline":
                await asyncio.Event().wait()
                return reply("Unreachable.")
            # Both modes advertise the same capability. The gate authorizes it at dispatch.
            assert "write_file" in request.tools
            if not request.contains("tool_result"):
                await release.wait()
                return tool_call(
                    "write_file",
                    {"path": "governed.txt", "content": "worker edit"},
                    use_id="governed-write",
                )
            assert request.contains("blocked_by_mode") is not can_write
            return reply(
                "The worker wrote its document."
                if can_write
                else "The effective mode denied the write."
            )

        async def main(request: ModelRequest, sequence: int) -> Completion:
            if sequence == 1:
                spawn = spawn_call(kind="explorer" if governance == "deadline" else "documenter")
                (call,) = spawn.tool_calls
                return replace(
                    spawn, tool_calls=[replace(call, input={**call.input, "timeout_s": 2.0})]
                )
            if sequence == 2:
                assert request.contains('"status": "running"')
                return reply("Allow the worker to settle independently.")
            if sequence == 3:
                return tool_call(
                    "collect_subagent_results",
                    {"pool_id": pool_id(session)},
                    use_id="governance-result",
                )
            if sequence == 4:
                if governance == "deadline":
                    assert request.contains("timed out")
                elif can_write:
                    assert request.contains("The worker wrote its document.")
                else:
                    assert request.contains("The effective mode denied the write.")
                return tool_call("mark_execute_complete", {"summary": "Handled worker policy."})
            assert sequence == 5
            return reply("Complete.")

        script = ScenarioTransport(main, worker)
        attach_phase(session, script)
        select_generate(session)
        task = await send(session, "Exercise the pool's worker policy in a workflow.")
        await until(lambda: session._workflow_handle is not None)
        handle = session._workflow_handle
        assert handle is not None
        conv = session._ctx.app_state.conversation
        await until(lambda: conv.phase_subagent_wait() == "waiting for phase subagents")
        if terminal_handoff:
            await detach_controller(session, tmp_path)
            assert session._workflow_handle is handle
        release.set()
        async with asyncio.timeout(8):
            await task
        written = Path("governed.txt")
        if can_write:
            assert written.read_text(encoding="utf-8") == "worker edit"
        else:
            assert not written.exists()
        assert len(script.main_requests) == 5
        assert handle.lifecycle == "complete"
        (record,) = source_for(session).journal.fold_subagent_pool_lifecycle()
        assert record["failed"] == int(governance != "capability-allowed")


async def test_a7_parallel_mutating_phase_workers_use_distinct_worktrees(
    phase_runtime: tuple[TUISession, bool],
    tmp_path: Path,
) -> None:
    session, detached = phase_runtime
    from tests.unit.test_subagent_worktrees import _git, _worktrees

    root = Path.cwd()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    notes = root / "notes.md"
    notes.write_text("Initial notes.", encoding="utf-8")
    _git(root, "add", "--", "notes.md")
    _git(root, "commit", "-q", "-m", "initial")
    notes.write_text("Uncommitted parent notes.", encoding="utf-8")
    edited: set[str] = set()
    release = asyncio.Event()

    async def worker(request: ModelRequest) -> Completion:
        description = str(request.messages[0]["content"])
        index = "0" if "create a chapter 0" in description else "1"
        if not request.contains("tool_result"):
            return tool_call("read_file", {"path": "notes.md"}, use_id=f"notes-{index}")
        assert request.contains("Uncommitted parent notes.")
        if not request.contains(f"chapter-{index}.md"):
            return tool_call(
                "write_file",
                {"path": f"chapter-{index}.md", "content": f"Chapter {index}"},
                use_id=f"write-{index}",
            )
        edited.add(index)
        await release.wait()
        return reply(f"Chapter {index} saved.")

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call(2, kind="implementer", task="create a chapter")
        if sequence == 2:
            assert request.contains('"status": "running"')
            return reply("Wait for the independent edits.")
        if sequence == 3:
            return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
        if sequence == 4:
            assert request.contains("Chapter 0 saved.") and request.contains("Chapter 1 saved.")
            return tool_call("mark_execute_complete", {"summary": "Applied both worker patches."})
        assert sequence == 5
        return reply("Done.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    select_generate(session)
    task = await send(session, "Create two separate chapter files.")
    await until(lambda: edited == {"0", "1"} or task.done())
    assert not task.done()
    worktrees = [Path(path) for path in _worktrees(root) if Path(path) != root]
    assert len(worktrees) == 2
    assert sorted(
        [path.name for path in worktree.glob("chapter-*.md")] for worktree in worktrees
    ) == [
        ["chapter-0.md"],
        ["chapter-1.md"],
    ]
    assert not list(root.glob("chapter-*.md"))
    handle = session._workflow_handle
    assert handle is not None
    if detached:
        await detach_controller(session, tmp_path)
        assert [Path(path) for path in _worktrees(root) if Path(path) != root] == worktrees
    release.set()
    async with asyncio.timeout(8):
        await task
    assert [path.read_text(encoding="utf-8") for path in sorted(root.glob("chapter-*.md"))] == [
        "Chapter 0",
        "Chapter 1",
    ]
    assert len(script.main_requests) == 5
    assert handle.lifecycle == "complete"
    assert source_for(session).journal.collected_phase_pools(handle.phase_invocation) == {
        pool_id(session)
    }
    assert _worktrees(root) == [str(root)]
    assert notes.read_text(encoding="utf-8") == "Uncommitted parent notes."


async def test_a2_a6_parked_phase_answers_input_while_pool_still_runs(
    session: TUISession,
) -> None:
    release = asyncio.Event()
    replied = asyncio.Event()
    Path("parent.txt").write_text("Parent-only follow-up evidence.", encoding="utf-8")

    async def worker(_: ModelRequest) -> Completion:
        await release.wait()
        return reply(WORKER_RESULT)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        if sequence == 1:
            return spawn_call()
        if sequence == 2:
            return reply("Waiting on the pool.")
        if sequence == 3:
            assert request.contains("Read the parent file first.")
            assert session._ctx.app_state.conversation.phase_subagent_wait() == ""
            assert not release.is_set()
            return tool_call("read_file", {"path": "parent.txt"}, use_id="queued-read")
        if sequence == 4:
            assert request.contains("Parent-only follow-up evidence.")
            assert repr(request.messages).count("Read the parent file first.") == 1
            assert not release.is_set()
            replied.set()
            return reply("I read the parent file. I will wait again.")
        if sequence == 5:
            return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
        if sequence == 6:
            assert request.contains(WORKER_RESULT)
            return tool_call("mark_execute_complete", {"summary": "All work collected."})
        assert sequence == 7
        return reply("Complete.")

    script = ScenarioTransport(main, worker)
    attach_phase(session, script)
    select_generate(session)
    task = await send(session, "Start and park a workflow pool.")
    conv = session._ctx.app_state.conversation
    await until(lambda: conv.phase_subagent_wait() == "waiting for phase subagents")
    handle = session._workflow_handle
    assert handle is not None
    await session.handle_send(SendMessageCommand(text="Read the parent file first."))
    await until(
        lambda: replied.is_set() and conv.phase_subagent_wait() == "waiting for phase subagents"
    )
    assert session._agent_task is task
    release.set()
    async with asyncio.timeout(8):
        await task
    assert handle.lifecycle == "complete"
    assert len(script.main_requests) == 7


async def test_hosted_pool_keeps_the_retry_budget_during_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lauren_ai._exceptions import TransientTransportError

    from agenthicc.runners import retry

    config = "[execution]\ntransport_max_retries = 2\ntransport_retry_base_delay_s = 0.001\n"
    async with tui(tmp_path, monkeypatch, config_text=config, host_controller=True) as session:
        at_backoff, continue_backoff = asyncio.Event(), asyncio.Event()
        observed_retries: list[int] = []
        attempts = 0
        run_with_retry = retry.run_with_transport_retry

        async def observe_attempt(turn, **options):
            if options.get("on_retry") is not None:
                return await run_with_retry(turn, **options)

            async def on_retry(
                attempt: int, limit: int, delay: float, error: BaseException
            ) -> None:
                assert limit == 2 and delay >= 0 and isinstance(error, TransientTransportError)
                observed_retries.append(attempt)
                at_backoff.set()
                await continue_backoff.wait()

            return await run_with_retry(turn, **options, on_retry=on_retry)

        monkeypatch.setattr(retry, "run_with_transport_retry", observe_attempt)

        async def worker(_: ModelRequest) -> Completion:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TransientTransportError("provider limit", status_code=429)
            return reply(WORKER_RESULT)

        async def main(request: ModelRequest, sequence: int) -> Completion:
            if sequence == 1:
                return spawn_call()
            if sequence == 2:
                return reply("Wait for the rate-limited worker.")
            if sequence == 3:
                return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
            if sequence == 4:
                assert request.contains(WORKER_RESULT)
                return transition()
            assert sequence == 5
            return reply("Collected after two provider retries.")

        script = ScenarioTransport(main, worker)
        attach_phase(session, script)
        select_generate(session)
        running = await send(session, "Exercise a rate-limited worker across detach.")
        await until(at_backoff.is_set)
        await detach_controller(session, tmp_path)
        assert observed_retries == [1] and attempts == 1
        assert not running.done()
        continue_backoff.set()
        async with asyncio.timeout(8):
            await running
        assert observed_retries == [1, 2] and attempts == 3
        worker_results = source_for(session).journal.fold_subagent_worker_results()
        assert len(worker_results) == 1 and worker_results[0]["ok"] is True
        assert worker_results[0].get("attempt", 1) == 1


async def test_controller_changes_while_pool_admission_and_sibling_exchange_are_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.background.store import BackgroundStore
    from agenthicc.runners.hosted_session import hosted_terminal_status
    from agenthicc.subagents.pool import SubagentPool

    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        admitting = asyncio.Event()
        release_admission = asyncio.Event()
        worker_running = asyncio.Event()
        release_worker = asyncio.Event()
        prepare_isolation = SubagentPool._prepare_isolation
        Path("independent.txt").write_text("independent sibling result", encoding="utf-8")

        async def prepare(pool, selected=None):
            admitting.set()
            await release_admission.wait()
            await prepare_isolation(pool, selected)

        monkeypatch.setattr(SubagentPool, "_prepare_isolation", prepare)

        async def worker(_: ModelRequest) -> Completion:
            worker_running.set()
            await release_worker.wait()
            return reply(WORKER_RESULT)

        async def main(request: ModelRequest, sequence: int) -> Completion:
            if sequence == 1:
                spawn = spawn_call()
                sibling = tool_call("read_file", {"path": "independent.txt"}, use_id="sibling")
                return replace(spawn, tool_calls=[*spawn.tool_calls, *sibling.tool_calls])
            if sequence == 2:
                assert request.contains('"status": "running"')
                assert request.contains("independent sibling result")
                return reply("The admitted pool is still working.")
            if not request.contains(WORKER_RESULT):
                return tool_call("collect_subagent_results", {"pool_id": pool_id(session)})
            if not request.contains("Implementation marked complete"):
                return tool_call("mark_execute_complete", {"summary": "Collected after detach"})
            return reply("Completed after the original pool exchange.")

        script = ScenarioTransport(main, worker)
        attach_phase(session, script)
        select_generate(session)
        running = await send(session, "Start an independent pool and read the source.")
        await until(admitting.is_set)
        await detach_controller(session, tmp_path)
        assert not running.done() and len(script.main_requests) == 1
        store = BackgroundStore(tmp_path / "background")
        record = store.get(session._ctx.session_id)
        assert (await hosted_terminal_status(record))["pools"] == []
        release_admission.set()
        await until(worker_running.is_set)
        await until(lambda: len(script.main_requests) == 2)
        release_worker.set()
        async with asyncio.timeout(8):
            await running
        assert len(script.main_requests) == 5
        assert len(script.worker_requests) == 1
        assert len(source_for(session).journal.fold_subagent_pool_lifecycle()) == 1


async def test_live_attach_rejects_old_controller_input_and_keeps_one_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.background.store import BackgroundStore
    from agenthicc.runners.hosted_transport import ControllerEndpoint, connect_controller

    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        entered, release = asyncio.Event(), asyncio.Event()

        async def main(_: ModelRequest, __: int) -> Completion:
            entered.set()
            await release.wait()
            return reply("original turn finished")

        async def worker(_: ModelRequest) -> Completion:
            pytest.fail("No worker is needed for a controller-fence test")

        attach(session, ScenarioTransport(main, worker))
        task = await send(session, "Hold a direct turn while controllers exchange authority.")
        await until(entered.is_set)
        await detach_controller(session, tmp_path)
        store = BackgroundStore(tmp_path / "background")
        original = store.get(session._ctx.session_id)
        assert original.live_handoff is not None
        endpoint = ControllerEndpoint.load(Path(original.artifact_dir) / ".controller")
        hello = {
            "type": "attach",
            "generation": original.live_handoff.generation,
            "columns": 160,
            "rows": 40,
        }
        client = await connect_controller(endpoint)
        try:
            await client.send(hello)
            first = await client.receive()
            assert first["type"] == "ready"
            active = store.get(original.session_id)
            assert active.worker_pid == original.worker_pid and active.attempt == original.attempt
            assert active.live_handoff is not None and active.live_handoff.generation == 2
            await client.send({"type": "key", "generation": 1, "key": "CTRL_C", "text": ""})
            assert await client.receive() == {"type": "rejected", "code": "stale_controller"}
            conflicting = await connect_controller(endpoint)
            try:
                await conflicting.send({**hello, "generation": 2})
                refusal = await conflicting.receive()
                assert refusal["type"] == "error" and "already owns" in str(refusal["message"])
            finally:
                await conflicting.close()
            assert not task.done()
        finally:
            await client.close()

        view = session._hosted_view
        assert view is not None
        await until(lambda: not view.attached)
        reconnect = await connect_controller(endpoint)
        try:
            await reconnect.send({**hello, "generation": 2})
            second = await reconnect.receive()
            assert second["type"] == "ready" and second["generation"] == 3
            assert store.get(original.session_id).attempt == original.attempt
            release.set()
            async with asyncio.timeout(8):
                await task
        finally:
            await reconnect.close()


async def test_terminal_bound_operation_blocks_cutover_until_it_releases_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background.store import BackgroundStore

    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        provider = asyncio.Event()
        editing, release_editor = asyncio.Event(), asyncio.Event()

        async def main(_: ModelRequest, __: int) -> Completion:
            await provider.wait()
            return reply("Finished")

        async def worker(_: ModelRequest) -> Completion:
            pytest.fail("No worker should be started by terminal interaction")

        async def edit() -> None:
            editing.set()
            await release_editor.wait()

        attach(session, ScenarioTransport(main, worker))
        turn = await send(session, "Keep the original turn running.")
        async with hosted_controller(session, tmp_path) as (view, stream, generation):
            operation = asyncio.create_task(view.run_terminal_operation(edit))
            try:
                await editing.wait()
                request = {"type": "detach", "generation": generation}
                await stream.send(request)
                assert await stream.receive() == {
                    "type": "rejected",
                    "code": "physical_terminal_required",
                }
                assert BackgroundStore(tmp_path / "background").active_sessions() == []
                assert not turn.done()
                release_editor.set()
                await operation
                await stream.send(request)
                accepted = await stream.receive()
                assert accepted["type"] == "detached"
            finally:
                release_editor.set()
                await operation
        provider.set()
        async with asyncio.timeout(8):
            await turn


async def test_live_approval_preserves_the_request_under_background_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.background.inbox import BackgroundInteractionInbox, InboxConflict
    from agenthicc.background.model import SessionStatus
    from agenthicc.background.store import BackgroundStore
    from agenthicc.tools.approval import ApprovalRequest, ApprovalRequester

    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        entered = asyncio.Event()

        async def main(_: ModelRequest, __: int) -> Completion:
            entered.set()
            await asyncio.Event().wait()
            return reply("Unreachable.")

        async def worker(_: ModelRequest) -> Completion:
            pytest.fail("The operator test constructs one request directly")

        attach(session, ScenarioTransport(main, worker))
        await send(session, "Keep this original turn alive during approval.")
        await entered.wait()
        request = ApprovalRequest(
            tool_name="write_file",
            tool_use_id="write-subagent",
            tool_input={"path": "fixture.txt"},
            capabilities=frozenset({"write"}),
            event=asyncio.Event(),
            request_id="approval-ac5",
            requester=ApprovalRequester(
                label="Executor 1",
                agent_type="executor",
                pool_id="fixture-pool",
                worker_id="fixture-worker",
            ),
        )
        approval = asyncio.create_task(session._ctx.approval_svc.request_approval(request))
        await until(lambda: session._ctx.app_state.pending_approval() is not None)
        await detach_controller(session, tmp_path)
        store = BackgroundStore(tmp_path / "background")
        session_id = session._ctx.session_id
        await until(lambda: store.get(session_id).status is SessionStatus.WAITING_APPROVAL)
        record = store.get(session_id)
        inbox = BackgroundInteractionInbox(store)
        await until(lambda: inbox.get(record.approval_item_id).status == "pending")
        item = inbox.get(record.approval_item_id)
        assert item.request_id == request.request_id
        assert item.target["requester"]["worker_id"] == "fixture-worker"
        inbox.respond(
            item.item_id,
            {"allowed": True},
            expected_version=item.version,
            idempotency_key="operator-wins",
        )
        with pytest.raises(InboxConflict):
            inbox.respond(
                item.item_id,
                {"allowed": False},
                expected_version=item.version,
                idempotency_key="late-offer",
            )
        async with asyncio.timeout(8):
            response = await approval
        assert response.allowed and response.request_id == request.request_id
        await until(lambda: inbox.get(item.item_id).status == "delivered")


@pytest.mark.parametrize(
    "frame,message",
    [
        ({"type": "resize", "columns": "wide", "rows": 30}, "Invalid terminal size"),
        ({"type": "resize", "columns": 10, "rows": 30}, "Terminal size outside supported limits"),
        ({"type": "key", "key": "CHAR", "text": 0}, "Invalid terminal input text"),
        ({"type": "key", "key": "BOGUS", "text": "x"}, "Unknown terminal key"),
        ({"type": "editor_done", "exit_status": 0}, "Invalid editor result"),
        ({"type": "invalid"}, "Invalid terminal input"),
    ],
)
async def test_hosted_controller_validates_input_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frame: dict[str, object],
    message: str,
) -> None:
    async with tui(tmp_path, monkeypatch, host_controller=True) as session:
        async with hosted_controller(session, tmp_path) as (view, stream, generation):
            await stream.send(
                {"type": "resize", "columns": 100, "rows": 35, "generation": generation}
            )
            await stream.send({**frame, "generation": generation})
            error = await stream.receive()
            assert error["type"] == "error" and error["message"] == message
            assert (view.columns, view.rows) == (100, 35)
            assert session._agent_task is None
