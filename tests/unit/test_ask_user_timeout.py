"""Regression coverage for PRD-192 ask_user timeout semantics."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.config import ToolSettings, load_config
from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.store import InvalidSessionTransition
from agenthicc.background.worker import BackgroundApprovalService
from agenthicc.tools.approval import ApprovalRequest, ApprovalService
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay
from agenthicc.workflows.code_plan.phase_tools import make_questions_tool

pytestmark = pytest.mark.unit


def _request(name: str, fingerprint: str = "question-a") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="ask_user",
        tool_use_id=name,
        tool_input={"questions": [{"id": "choice", "text": "Choose", "options": ["A", "B"]}]},
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="questions",
        question_fingerprint=fingerprint,
    )


def _background_record(tmp_path: Path) -> BackgroundSession:
    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    return BackgroundSession.create(
        "question-session",
        title="Question",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="test",
        artifact_dir=str(artifact),
    )


@pytest.mark.asyncio
async def test_question_timeout_is_structured_and_clears_pending_state() -> None:
    app = AppState.create()
    service = ApprovalService(app, question_timeout_s=0.02)
    task = asyncio.create_task(service.request_approval(_request("timeout")))

    response = await asyncio.wait_for(task, timeout=1)

    assert response.allowed is False
    assert response.timed_out is True
    assert response.outcome == "timed_out"
    assert response.decision_required is True
    assert response.request_id == "timeout"
    assert response.timeout_s == 0.02
    assert app.pending_approval() is None
    kinds = [event.kind for turn in app.conversation.turns() for event in turn.events]
    # The lifecycle remains observable even when the caller has no active turn.
    assert kinds == []  # no current turn means no visual event projection


@pytest.mark.asyncio
async def test_question_lifecycle_events_are_bounded_and_idempotent() -> None:
    app = AppState.create()
    app.conversation.begin_turn("assistant", "turn-1")
    service = ApprovalService(app, question_timeout_s=0.02)
    response = await service.request_approval(_request("eventful"))
    assert response.timed_out

    events = app.conversation._current_turn.events  # type: ignore[union-attr]
    assert [event.kind for event in events] == [
        "question_wait_started",
        "question_timed_out",
    ]
    assert events[0].payload["question_count"] == 1
    assert "text" not in events[0].payload
    assert "answers" not in events[1].payload

    # A duplicate projection with the same lifecycle identity is ignored.
    app.conversation.append_event(
        "question_timed_out",
        dict(events[1].payload),
        event_id=events[1].event_id,
    )
    assert len(app.conversation._current_turn.events) == 2  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_answer_before_deadline_wins_and_is_returned_once() -> None:
    app = AppState.create()
    service = ApprovalService(app, question_timeout_s=0.2)
    request = _request("answered", "answer-fingerprint")
    task = asyncio.create_task(service.request_approval(request))
    await asyncio.sleep(0)

    assert service.respond_for_request("answered", True, message='{"choice":"A"}') is True
    response = await asyncio.wait_for(task, timeout=1)

    assert response.allowed is True
    assert response.timed_out is False
    assert response.outcome == "answered"
    assert app.pending_approval() is None
    assert service.respond_for_request("answered", True, message='{"choice":"B"}') is False


@pytest.mark.asyncio
async def test_question_overlay_renders_remaining_deadline_without_transcript_content() -> None:
    app = AppState.create()
    service = ApprovalService(app, question_timeout_s=0.2)
    request = _request("countdown", "countdown-fingerprint")
    task = asyncio.create_task(service.request_approval(request))
    await asyncio.sleep(0)
    overlay = QuestionsOverlay(request, service, lambda: None)
    console = Console(
        record=True,
        force_terminal=True,
        width=80,
        color_system=None,
        highlight=False,
    )
    console.print(overlay.render())

    rendered = console.export_text()
    assert "remaining" in rendered
    assert "Choose" in rendered
    service.respond_for_request("countdown", False, outcome="cancelled")
    await task


@pytest.mark.asyncio
async def test_late_overlay_response_cannot_answer_next_question() -> None:
    app = AppState.create()
    service = ApprovalService(app, question_timeout_s=0.02)
    first = await service.request_approval(_request("first", "first-fingerprint"))
    assert first.timed_out

    second_request = _request("second", "second-fingerprint")
    second_task = asyncio.create_task(service.request_approval(second_request))
    await asyncio.sleep(0)

    assert service.respond_for_request("first", True, message='{"choice":"stale"}') is False
    assert not second_task.done()
    assert service.respond_for_request("second", True, message='{"choice":"fresh"}') is True
    second = await asyncio.wait_for(second_task, timeout=1)
    assert second.outcome == "answered"
    assert second.request_id == "second"


@pytest.mark.asyncio
async def test_repeated_identical_question_is_bounded_after_timeout() -> None:
    app = AppState.create()
    service = ApprovalService(app, question_timeout_s=0.01)
    first = await service.request_approval(_request("first", "same"))
    assert first.timed_out and not first.repeated_timeout

    second = await asyncio.wait_for(
        service.request_approval(_request("second", "same")), timeout=0.2
    )
    assert second.timed_out is True
    assert second.repeated_timeout is True
    assert app.pending_approval() is None


@pytest.mark.asyncio
async def test_make_questions_tool_exposes_timeout_without_fabricating_answers() -> None:
    metadata: dict[str, object] = {}

    class _TimedOut:
        async def request_approval(self, _request: object) -> object:
            return SimpleNamespace(
                allowed=False,
                timed_out=True,
                decision_required=True,
                request_id="request-1",
                timeout_s=60.0,
                repeated_timeout=False,
                message="choose safely",
            )

    ask = make_questions_tool(_TimedOut(), metadata)[0]
    result = await ask([{"id": "choice", "text": "Choose", "options": ["A"]}])

    assert result == {
        "timed_out": True,
        "decision_required": True,
        "request_id": "request-1",
        "timeout_s": 60.0,
        "repeated_timeout": False,
        "message": "choose safely",
    }
    assert metadata["status"] == "timed_out"
    assert metadata["fallback_required"] is True
    assert "choice" not in result


def test_question_timeout_configuration_defaults_and_loads_from_toml(tmp_path) -> None:
    assert ToolSettings().question_timeout_s == 60.0
    config_path = tmp_path / "agenthicc.toml"
    config_path.write_text("[tools]\nquestion_timeout_s = 180\n", encoding="utf-8")
    config = load_config(project_path=config_path, user_path=tmp_path / "missing.toml")
    assert config.tools.question_timeout_s == 180.0
    cli_config = load_config(
        project_path=config_path,
        user_path=tmp_path / "missing.toml",
        cli_overrides=["tools.question_timeout_s=240"],
    )
    assert cli_config.tools.question_timeout_s == 240.0


@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, "not-a-number"])
def test_question_timeout_configuration_rejects_unbounded_or_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="tools.question_timeout_s"):
        ToolSettings(question_timeout_s=value)  # type: ignore[arg-type]


def test_rehydrated_expired_question_is_reconciled_before_overlay() -> None:
    app = AppState.create()
    service = ApprovalService(
        app,
        question_timeout_s=60.0,
        question_wait_records={
            "rehydrate": {
                "request_id": "old-request",
                "question_fingerprint": "rehydrate",
                "deadline_at": 1.0,
                "timeout_s": 60.0,
            }
        },
    )

    async def run() -> object:
        return await service.request_approval(_request("new-request", "rehydrate"))

    response = asyncio.run(run())
    assert response.timed_out is True
    assert response.repeated_timeout is True
    assert app.pending_approval() is None


@pytest.mark.asyncio
async def test_background_question_timeout_releases_waiting_session(tmp_path: Path) -> None:
    app = AppState.create()
    app.conversation.begin_turn("assistant", "background-question-turn")
    store = BackgroundStore(tmp_path / "background")
    store.create(_background_record(tmp_path))
    store.claim("question-session", pid=1, lease_token="question-lease")
    service = BackgroundApprovalService(
        store,
        "question-session",
        question_timeout_s=0.01,
        conversation_store=app.conversation,
    )

    response = await service.request_input(
        SimpleNamespace(tool_name="Questions", request_id="background-question")
    )

    assert response.timed_out is True
    assert response.decision_required is True
    assert response.request_id == "background-question"
    assert store.get("question-session").status is SessionStatus.RUNNING
    assert [event.kind for event in app.conversation._current_turn.events] == [  # type: ignore[union-attr]
        "question_wait_started",
        "question_timed_out",
    ]
    with pytest.raises(InvalidSessionTransition):
        service.provide_input("late answer")
