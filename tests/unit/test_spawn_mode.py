"""Spawn policy, operator configuration and stable model guidance."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport._mock import MockTransport

from agenthicc.cli.commands.config import config_show
from agenthicc.cli.context import CLIContext
from agenthicc.config import SubagentSettings, load_config
from agenthicc.memory.journal import ConversationJournal
from agenthicc.runners.agent_turn_context import bind_background_task_owner, bind_turn_notices
from agenthicc.runners.workflow_execution_context import bind_workflow_execution
from agenthicc.subagents.communication import PoolContinuationRegistry
from agenthicc.subagents.delivery import JournalNoticeSource
from agenthicc.subagents.spawn_mode import (
    BACKGROUND_PROMPT,
    background_receipt_text,
    resolve_spawn_mode,
)
from agenthicc.subagents.tool import SubagentToolSelection, make_spawn_subagents_tool
from agenthicc.supervised_tasks import SupervisedTasks
from agenthicc.tools.surface import ToolSurface
from tests.e2e.self_extension.harness import reply


@pytest.mark.parametrize(
    ("setting", "side", "owner", "journal_bound", "notice_bound", "expected"),
    [
        ("background", "direct", True, True, True, "background"),
        ("foreground", "direct", True, True, True, "foreground"),
        ("background", "unsupported-phase", True, True, True, "foreground"),
        ("background", "eligible-phase", True, True, True, "background"),
        ("foreground", "eligible-phase", True, True, True, "foreground"),
        ("background", "eligible-phase", False, True, True, "foreground"),
        ("background", "eligible-phase", True, False, True, "foreground"),
        ("background", "eligible-phase", True, True, False, "foreground"),
        ("background", "headless", False, True, False, "foreground"),
        ("background", "service", False, True, False, "foreground"),
        ("background", "direct", False, True, True, "foreground"),
        ("background", "direct", True, False, True, "foreground"),
        ("background", "direct", True, True, False, "foreground"),
    ],
)
def test_a12_mode_requires_all_delivery_dependencies(
    tmp_path: Path,
    setting: str,
    side: str,
    owner: bool,
    journal_bound: bool,
    notice_bound: bool,
    expected: str,
) -> None:
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    supervisor = SupervisedTasks("test")
    try:
        assert (
            resolve_spawn_mode(
                SubagentSettings(spawn_mode=setting),
                owner=supervisor if owner else None,
                journal=journal if journal_bound else None,
                notices=JournalNoticeSource(journal, PoolContinuationRegistry())
                if notice_bound
                else None,
                in_workflow_phase=side.endswith("-phase"),
                phase_delivery=side == "eligible-phase",
            )
            == expected
        )
    finally:
        journal.close()


@pytest.mark.parametrize("context", ["phase", "headless", "service", "operator"])
async def test_a12_foreground_call_waits_for_the_worker(
    tmp_path: Path,
    context: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Worker(MockTransport):
        async def complete(self, *args: object, **kwargs: object) -> object:
            entered.set()
            await release.wait()
            self.queue_response(reply("Worker finished."))
            return await super().complete(*args, **kwargs)

    owner = SupervisedTasks("test foreground")
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    source = JournalNoticeSource(journal, PoolContinuationRegistry())
    settings = SubagentSettings(spawn_mode="foreground" if context == "operator" else "background")
    supervised = context in {"phase", "operator"}
    try:
        with (
            bind_background_task_owner(owner, interactive=True) if supervised else nullcontext(),
            bind_turn_notices(source if supervised else None),
            bind_workflow_execution("phase-run") if context == "phase" else nullcontext(),
        ):
            spawn = make_spawn_subagents_tool(
                AgentRunnerBase(transport=Worker()),
                "offline",
                SubagentToolSelection(ToolSurface(0, ())),
                subagent_settings=settings,
                conversation_id="conv",
                conversation_journal=journal,
            )
            metadata = object.__getattribute__(spawn, "__lauren_ai_tool__")
            assert "Blocks until the pool finishes" in metadata.description
            assert "background" not in metadata.parameters["input_schema"]["properties"]
            task = asyncio.create_task(spawn(tasks=[{"type": "explorer", "task": "Inspect."}]))
            async with asyncio.timeout(5):
                await entered.wait()
            assert not task.done()
            release.set()
            result = await task
        assert result["status"] == "completed" and "Worker finished." in result["results"]
        assert journal.fold_subagent_pool_lifecycle()[0]["backgrounded"] is False
    finally:
        release.set()
        await owner.aclose()
        journal.close()


def test_a17_spawn_mode_defaults_validates_and_prints_in_config_show(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert SubagentSettings().spawn_mode == "background"
    with pytest.raises(ValueError, match=r"subagents\.spawn_mode"):
        SubagentSettings(spawn_mode="invalid")
    config = tmp_path / "agenthicc.toml"
    for setting in ("background", "foreground"):
        config.write_text(f'[subagents]\nspawn_mode = "{setting}"\n', encoding="utf-8")
        assert load_config(config_path=str(config)).subagents.spawn_mode == setting
        config_show(CLIContext(config_path=str(config)))
        assert f"spawn_mode = '{setting}'" in capsys.readouterr().out
    config.write_text("[subagents]\nbackground_enabled = false\n", encoding="utf-8")
    with pytest.raises(
        ValueError, match=r"background_enabled was replaced by subagents\.spawn_mode"
    ):
        load_config(config_path=str(config))
    config.write_text("[subagents]\nspawn_mode = true\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"subagents\.spawn_mode"):
        load_config(config_path=str(config))


def test_a18_stable_background_guidance_and_receipt() -> None:
    assert (
        BACKGROUND_PROMPT
        == """## Subagents
spawn_subagents does not block you. After spawning, keep working on other tasks
or end your turn; the runtime resumes you with a notice when the pool finishes.
Then call collect_subagent_results(pool_id). Never poll, sleep, or repeat a
worker's task. Never describe or guess a pending worker's results; if asked,
say it is still running."""
    )
    assert (
        background_receipt_text("example", 3)
        == """Subagent pool example is running in the background (3 workers). Your turn
is not blocked: continue with anything that does not need their results, or end
your turn. You will be resumed automatically when the pool finishes. Do not
poll, sleep, or redo their work, and do not state or guess what they will find
until you have collected the results."""
    )
