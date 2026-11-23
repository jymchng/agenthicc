"""Wake scheduling, draft ownership, and usage projections."""

from __future__ import annotations

from pathlib import Path

import pytest
from lauren_ai._transport import TokenUsage

from agenthicc.memory.journal import ConversationJournal
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.tui_session import TUISession
from agenthicc.runners.usage_ledger import UsageCategory, UsageLedger, UsageRunTracker
from agenthicc.tui.input.unified_session import InputMode, UnifiedInputSession
from tests.integration.test_subagent_wake import record_terminal
from tests.unit.test_tui_session_coverage import _make_session


async def test_a7_a8_reconciliation_uses_the_idle_scheduler_and_preserves_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, ctx, _workspace, _input = _make_session()
    assert isinstance(session, TUISession)
    conversation = SessionConversation.open(
        ctx.session_id,
        max_tokens=5_000,
        journal_path=tmp_path / "journal.jsonl",
    )
    ctx.session_conversation = conversation
    ctx.session_memory = conversation.memory
    input_session = UnifiedInputSession(ctx.app_state, ctx.command_bus)
    input_session.set_text("typing a draft")
    monkeypatch.setattr(session, "_input_session", input_session)
    attempts: list[str] = []

    async def run(text: str, *, wake_key: str) -> None:
        assert text == ""
        assert input_session._mode == InputMode.STREAMING
        assert "".join(ctx.app_state.input.buf()) == "typing a draft"
        attempts.append(wake_key)
        input_session.set_mode(InputMode.IDLE)

    monkeypatch.setattr(session, "run_turn", run)
    try:
        source = session._install_subagent_notices()
        assert source is not None
        record_terminal(source.journal, "recovered")
        session.reconcile_subagent_wakes()
        first = session._agent_task
        assert first is not None
        session.reconcile_subagent_wakes()
        assert session._agent_task is first
        await first
        session.reconcile_subagent_wakes()
        assert session._agent_task is None
        assert len(attempts) == session._subagent_wake_recoveries == 1
        assert "".join(ctx.app_state.input.buf()) == "typing a draft"
        assert not session._msg_queue
    finally:
        conversation.close()


def test_a20_wake_usage_attribution_survives_reopen_and_usage_reports_separately(
    tmp_path: Path,
) -> None:
    session, ctx, _workspace, _input = _make_session()
    assert isinstance(session, TUISession)
    from agenthicc.commands.builtins import _cmd_usage
    from agenthicc.commands.command import CommandContext

    path = tmp_path / "usage.jsonl"
    journal = ConversationJournal(path)
    try:
        ledger = UsageLedger(ctx.session_id, journal=journal)
        for run, category, tokens in [
            ("ordinary", UsageCategory.AGENT, 3),
            ("subagent-wake:notice", UsageCategory.WAKE, 9),
        ]:
            tracker = UsageRunTracker(
                ledger, run_id=run, provider="test", model="offline", category=category
            )
            tracker.observe_chunk(TokenUsage(input_tokens=tokens, output_tokens=1))
            tracker.finalize("completed")
    finally:
        journal.close()

    reopened = ConversationJournal(path)
    try:
        ctx.usage_ledger = UsageLedger(ctx.session_id, journal=reopened)
        assert ctx.usage_ledger.records()[-1].category == UsageCategory.WAKE
        assert _cmd_usage(
            CommandContext(
                text="/usage",
                args="",
                model="offline",
                console=ctx.console,
                config=ctx.cfg,
                usage_snapshot=session._usage_snapshot,
            )
        )
        assert (
            "Ordinary agent turns: 4 tokens. Subagent wakes: 10 tokens (1 call)."
            in ctx.console.export_text()
        )
    finally:
        reopened.close()
