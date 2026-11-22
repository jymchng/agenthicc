"""SUBAGENT-14: the parent sees what the runtime observed each worker do."""

from __future__ import annotations

from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.memory.journal import ConversationJournal
from agenthicc.subagents.pool import SubagentResult, _aggregate, evidence_footer
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import DEFAULT_REGISTRY, SubagentAggregator, SubagentTypeRegistry
from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspaceScope,
    reset_current_workspace_access,
    set_current_workspace_access,
)
from agenthicc.tui.conversation_store import AppState, ConversationStore
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit


def _result(**overrides: object) -> SubagentResult:
    fields: dict[str, object] = {
        "task_id": "task-0",
        "agent_type": "implementer",
        "label": "implementer #1",
        "ok": True,
        "text": "Done.",
    }
    fields.update(overrides)
    return SubagentResult(**fields)  # type: ignore[arg-type]


# ── the formatter ────────────────────────────────────────────────────────────


def test_a_worker_that_ran_nothing_and_answered_once_has_no_footer() -> None:
    assert evidence_footer(_result(turns=1)) == ""
    assert evidence_footer(_result(turns=0)) == ""


def test_a_worker_that_was_never_started_has_no_footer() -> None:
    assert evidence_footer(_result(not_started=True, tool_calls=("read_file",), turns=3)) == ""


def test_the_footer_names_changes_tools_refusals_and_turns() -> None:
    footer = evidence_footer(
        _result(
            changed_paths=("chapters/01.md", "chapters/02.md"),
            tool_calls=("read_file", "run_tests", "grep_files", "run_tests"),
            blocked_calls=("write_file: approval_denied",),
            turns=7,
        )
    )
    assert footer == (
        "[evidence: changed chapters/01.md, chapters/02.md · "
        "ran run_tests ×2, read_file, grep_files · "
        "refused write_file: approval_denied · 7 turns]"
    )


def test_more_than_five_changed_paths_render_five_and_a_count() -> None:
    paths = tuple(f"src/m{index}.py" for index in range(8))

    footer = evidence_footer(_result(changed_paths=paths, tool_calls=("write_file",), turns=2))

    assert "changed src/m0.py, src/m1.py, src/m2.py, src/m3.py, src/m4.py (+3 more)" in footer
    assert "src/m5.py" not in footer


def test_tools_rank_by_count_then_first_use_and_are_capped() -> None:
    calls = ("c", "a", "b", "a", "d", "e", "f", "g", "h")  # a ×2; the rest once, in use order

    footer = evidence_footer(_result(tool_calls=calls, turns=2))

    assert "ran a ×2, c, b, d, e, f (+2 more)" in footer


def test_a_long_or_multiline_name_is_flattened_and_bounded() -> None:
    hostile = "weird\n\nname " + "x" * 400

    footer = evidence_footer(_result(changed_paths=(hostile,), tool_calls=("write_file",), turns=2))

    assert "\n" not in footer
    assert max(len(part) for part in footer.split(" · ")) < 200
    assert "…" in footer


def test_the_footer_is_deterministic() -> None:
    result = _result(
        changed_paths=("a.py",),
        tool_calls=("x", "y", "x"),
        blocked_calls=("w: blocked_by_mode",),
        turns=4,
    )
    assert evidence_footer(result) == evidence_footer(result)


# ── the digest ───────────────────────────────────────────────────────────────


def test_the_default_digest_carries_the_footer_after_the_workers_prose() -> None:
    result = _result(changed_paths=("a.py",), tool_calls=("write_file",), turns=2)

    digest = _aggregate("pool", [result], DEFAULT_REGISTRY).text

    assert digest.endswith("Done.\n[evidence: changed a.py · ran write_file · 2 turns]")


def test_a_failed_worker_shows_its_refusal_and_no_changes() -> None:
    result = _result(
        ok=False,
        text="I wrote out.txt.",
        error="write_file was refused (approval_denied); no change was made",
        tool_calls=("write_file",),
        blocked_calls=("write_file: approval_denied",),
        turns=2,
    )

    digest = _aggregate("pool", [result], DEFAULT_REGISTRY).text

    assert "refused write_file: approval_denied" in digest
    assert "changed" not in digest.rsplit("[evidence:", 1)[1]


def test_a_custom_aggregator_keeps_full_control_of_its_section() -> None:
    class Terse(SubagentAggregator):
        agent_type = "implementer"

        def aggregate(self, results: list[SubagentResult]) -> str:
            return "custom"

    registry = SubagentTypeRegistry()
    registry.register_aggregator(Terse())
    result = _result(changed_paths=("a.py",), tool_calls=("write_file",), turns=2)

    digest = _aggregate("pool", [result], registry).text

    assert "custom" in digest
    assert "[evidence:" not in digest


# ── end to end: real workers, real policy ────────────────────────────────────


def _final(text: str) -> Completion:
    return Completion(
        id="final",
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


class _Broker:
    def __init__(self, allow: bool) -> None:
        self.allow = allow

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(allowed=self.allow, outcome="answered")

    def reset_turn_memory(self) -> None:
        return None


async def _run(
    tmp_path: Path, mode: str, allow: bool, journal: ConversationJournal | None = None
) -> tuple[dict[str, object], list[object]]:
    transport = MockTransport()
    transport.queue_tool_use(
        "write_file", {"path": "out.txt", "content": "hello"}, tool_use_id="w1"
    )
    transport.queue_response(_final("I wrote out.txt."))
    state = AppState.create()
    state.active_mode.set(build_default_registry().get(mode))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(tmp_path), mode_provider=state.active_mode)
    store = ConversationStore()
    store.begin_turn("agent", "turn-1")
    seen: list[object] = []
    store.on_event(seen.append)
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        list(AGENT_TOOLS),
        app_state=state,
        workspace_access=policy,
        approval_svc=_Broker(allow),
        conv_store=store,
        conversation_journal=journal,
    )
    token = set_current_workspace_access(policy)
    try:
        result = await spawn(
            tasks=[{"type": "implementer", "task": "Write out.txt."}], timeout_s=30
        )
    finally:
        reset_current_workspace_access(token)
    return result, seen  # type: ignore[return-value]


def _worker_done(events: list[object]) -> dict[str, object]:
    payloads = [
        event.payload  # type: ignore[attr-defined]
        for event in events
        if getattr(event, "kind", "") in {"subagent_worker_done", "subagent_worker_failed"}
    ]
    assert len(payloads) == 1
    return dict(payloads[0])


async def test_a_successful_write_is_evidence_in_the_digest_and_the_event(tmp_path: Path) -> None:
    result, events = await _run(tmp_path, "Yolo", True)

    assert result["ok"] is True
    assert "[evidence: changed out.txt · ran write_file · 2 turns]" in str(result["results"])
    payload = _worker_done(events)
    assert payload["changed_paths"] == ["out.txt"]
    assert payload["blocked_calls"] == []
    assert payload["turns"] == 2


async def test_a_denied_write_is_a_refusal_in_the_digest_and_the_event(tmp_path: Path) -> None:
    result, events = await _run(tmp_path, "Safe", False)

    assert result["ok"] is False
    text = str(result["results"])
    assert "refused write_file: approval_denied" in text
    assert "changed" not in text.rsplit("[evidence:", 1)[1]
    payload = _worker_done(events)
    assert payload["changed_paths"] == []  # a refused write is not a change
    assert payload["blocked_calls"] == ["write_file: approval_denied"]
    assert not (tmp_path / "out.txt").exists()


async def test_the_journal_record_keeps_refusals_and_turns(tmp_path: Path) -> None:
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    try:
        await _run(tmp_path, "Safe", False, journal)
        record = journal.fold_subagent_worker_results()[0]
    finally:
        journal.close()

    assert record["blocked_calls"] == ["write_file: approval_denied"]
    assert record["turns"] == 2


async def test_a_journal_record_written_before_these_fields_still_reads(tmp_path: Path) -> None:
    journal = ConversationJournal(tmp_path / "conversation-journal.jsonl")
    try:
        journal.subagent_worker_result(
            pool_id="p",
            fingerprint="f",
            task_id="task-0",
            agent_type="implementer",
            label="implementer #1",
            ok=True,
            text="done",
            error="",
            duration_ms=1.0,
            tool_calls=["write_file"],
            changed_paths=["a.py"],
        )
        record = journal.fold_subagent_worker_results()[0]
    finally:
        journal.close()

    assert "blocked_calls" not in record and "turns" not in record
    assert record["changed_paths"] == ["a.py"]
