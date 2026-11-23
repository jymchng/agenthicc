"""Resolve how a turn can own and receive results from a subagent pool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agenthicc.config import SubagentSettings
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.subagents.delivery import NoticeSource
    from agenthicc.supervised_tasks import SupervisedTasks

SpawnMode = Literal["background", "foreground"]

BACKGROUND_PROMPT = """## Subagents
spawn_subagents does not block you. After spawning, keep working on other tasks
or end your turn; the runtime resumes you with a notice when the pool finishes.
Then call collect_subagent_results(pool_id). Never poll, sleep, or repeat a
worker's task. Never describe or guess a pending worker's results; if asked,
say it is still running."""


def resolve_spawn_mode(
    settings: SubagentSettings,
    *,
    owner: SupervisedTasks | None,
    journal: ConversationJournal | None,
    notices: NoticeSource | None,
    in_workflow_phase: bool,
    phase_delivery: bool = False,
) -> SpawnMode:
    """Resolve the operator setting against delivery capabilities of the turn."""
    if (
        settings.spawn_mode == "background"
        and owner is not None
        and journal is not None
        and notices is not None
        and (not in_workflow_phase or phase_delivery)
    ):
        return "background"
    return "foreground"


def current_spawn_mode(
    settings: SubagentSettings, journal: ConversationJournal | None
) -> SpawnMode:
    from agenthicc.runners.agent_turn_context import (
        current_background_task_owner,
        current_notice_source,
    )
    from agenthicc.runners.workflow_execution_context import current_workflow_run_id
    from agenthicc.subagents.phase import current_phase_pools

    phase = current_phase_pools()
    return resolve_spawn_mode(
        settings,
        owner=current_background_task_owner(),
        journal=journal,
        notices=current_notice_source(),
        in_workflow_phase=current_workflow_run_id() is not None,
        phase_delivery=phase is not None and phase.available,
    )


def background_receipt_text(pool_id: str, workers: int) -> str:
    return (
        f"Subagent pool {pool_id} is running in the background ({workers} workers). Your turn\n"
        "is not blocked: continue with anything that does not need their results, or end\n"
        "your turn. You will be resumed automatically when the pool finishes. Do not\n"
        "poll, sleep, or redo their work, and do not state or guess what they will find\n"
        "until you have collected the results."
    )
