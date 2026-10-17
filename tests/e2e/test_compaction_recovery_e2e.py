"""Detached-session end-to-end recovery coverage for PRD-242."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.worker import WorkerRequest, run_worker
from agenthicc.memory.journal import ConversationJournal, fold_resume_state
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.runners.agent_turn import AgentTurnRunner
from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.e2e


class _SmallExecutionConfig:
    auto_compact = True
    max_output_tokens = 100
    transport_max_retries = 0
    transport_retry_base_delay_s = 0.0
    transport_retry_max_total_s = 0.0

    @staticmethod
    def effective_context_window() -> int:
        return 1_000

    @staticmethod
    def effective_usable_budget() -> int:
        return 800


class _Processor:
    async def run(self) -> None:
        await asyncio.Event().wait()

    async def drain(self) -> None:
        return None


class _SummaryTransport:
    async def complete(self, _messages, **_kwargs):  # noqa: ANN001
        return SimpleNamespace(content="Summary of the prior conversation.")


async def test_detached_direct_compaction_failure_is_recoverable_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agenthicc.runners.headless as headless
    import agenthicc.runners.tui_session as tui_session
    import agenthicc.tui.runtime.session_log as session_log

    session_id = "compact-direct-e2e"
    artifact_dir = tmp_path / "sessions" / session_id
    artifact_dir.mkdir(parents=True)
    journal_path = artifact_dir / "conversation-journal.jsonl"
    store = BackgroundStore(tmp_path / "background")
    store.create(
        BackgroundSession.create(
            session_id,
            title="Compaction recovery",
            cwd=str(tmp_path),
            workflow_name="",
            intent="accepted request that must remain resumable",
            artifact_dir=str(artifact_dir),
        )
    )
    sessions: list[SimpleNamespace] = []

    async def build(*_args: object, **_kwargs: object) -> SimpleNamespace:
        journal = ConversationJournal(journal_path)
        memory = JournaledShortTermMemory(journal, max_tokens=800)
        memory.add_user("Prior conversation " + "history " * 350)
        app_state = SimpleNamespace(
            conversation=ConversationStore(),
            workflow_run=lambda: None,
            cli_flags=None,
        )
        session = SimpleNamespace(
            session_id=session_id,
            processor=_Processor(),
            session_memory=memory,
            app_state=app_state,
            cfg=SimpleNamespace(),
            mode_manager=SimpleNamespace(active_name="Yolo"),
        )
        sessions.append(session)
        return session

    async def close(_session: object, processor_task: object, _registry: object) -> None:
        task = processor_task
        if isinstance(task, asyncio.Task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for session in sessions:
            memory = session.session_memory
            journal = memory._journal
            journal.close()

    async def direct_turn(session: object, request: WorkerRequest, **_kwargs: object) -> None:
        memory = session.session_memory
        context = AgentTurnContext(
            text=request.intent,
            runner=SimpleNamespace(_transport=_SummaryTransport(), _signals=None),  # type: ignore[arg-type]
            processor=session.processor,
            session_memory=memory,
            conversation_id=session.session_id,
            max_agent_turns=1,
            conv_store=session.app_state.conversation,
            app_state=session.app_state,
            exec_cfg=_SmallExecutionConfig(),  # type: ignore[arg-type]
            active_agent="default",
            completed_turns=0,
            output_collector=[],
        )
        turn = AgentTurnRunner(context)
        turn._intent_id = "detached-direct-turn"
        turn._model_id = "offline-test"
        await turn._stream(
            object(),
            request.intent,
            SimpleNamespace(_transport=_SummaryTransport()),  # type: ignore[arg-type]
        )

    def fail_durable_replace(_journal, _messages, _summary) -> None:
        raise OSError("simulated compaction journal failure")

    monkeypatch.setattr(tui_session, "_build_session_context", build)
    monkeypatch.setattr(headless, "_close_headless_session", close)
    monkeypatch.setattr(session_log, "register_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agenthicc.background.worker._run_direct_turn", direct_turn)
    monkeypatch.setattr(ConversationJournal, "replace_state", fail_durable_replace)

    request = WorkerRequest(
        session_id,
        "",
        "accepted request that must remain resumable",
        str(tmp_path),
        None,
        (),
        False,
        detached_goal=True,
    )
    assert await run_worker(request, store) == 0

    recovered = store.get(session_id)
    assert recovered.status is SessionStatus.ORPHANED
    assert recovered.failure_category == "compaction_persistence_failed"
    assert recovered.worker_exit_reason == "compaction_persistence_failed"

    reopened_journal = ConversationJournal(journal_path)
    reopened_memory = JournaledShortTermMemory(reopened_journal, max_tokens=800)
    assert len(reopened_memory._messages) == 1
    pending_turn = fold_resume_state(journal_path)
    assert pending_turn is not None
    assert pending_turn.turn_id == "detached-direct-turn"
    assert pending_turn.user_message == "accepted request that must remain resumable"
    reopened_journal.close()
