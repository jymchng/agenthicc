"""Unit tests: PlanApprovalOverlay in a Dialog (TUX-07) and the ApprovalRequest/Response model."""

from __future__ import annotations

import asyncio
import os

import pytest
from rich.console import Console, Group
from unittest.mock import MagicMock

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.dialog import Dialog
from agenthicc.tui.workspace.overlays.plan_approval import (
    KEEP_PLANNING_MESSAGE,
    PlanApprovalOverlay,
)
from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse, ApprovalService

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_req(
    plan: str = "Step 1\nStep 2\nStep 3",
    mode_options: tuple[str, ...] = (),
) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="Review: plan",
        tool_use_id="abc",
        tool_input={"plan": plan},
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="plan_review",
        mode_options=mode_options,
    )


def _make_overlay(plan: str = "Step 1\nStep 2") -> tuple[PlanApprovalOverlay, MagicMock, MagicMock]:
    req = _make_req(plan)
    service = MagicMock()
    close_fn = MagicMock()
    ov = PlanApprovalOverlay(req, service, close_fn)
    ov.on_mount()
    return ov, service, close_fn


def _make_mode_overlay(
    plan: str = "Step 1\nStep 2",
) -> tuple[PlanApprovalOverlay, MagicMock, MagicMock]:
    req = _make_req(plan, mode_options=("Safe", "Yolo"))
    service = MagicMock()
    close_fn = MagicMock()
    ov = PlanApprovalOverlay(req, service, close_fn)
    ov.on_mount()
    return ov, service, close_fn


# ── ApprovalRequest / ApprovalResponse data-model ────────────────────────────


class TestDataModel:
    def test_request_default_kind_is_tool(self):
        req = ApprovalRequest(
            tool_name="write_file",
            tool_use_id="x",
            tool_input={},
            capabilities=frozenset(),
            event=asyncio.Event(),
        )
        assert req.kind == "tool"

    def test_request_plan_review_kind(self):
        req = _make_req()
        assert req.kind == "plan_review"

    def test_response_default_message_empty(self):
        resp = ApprovalResponse(allowed=True)
        assert resp.message == ""

    def test_response_with_message(self):
        resp = ApprovalResponse(allowed=False, message="needs error handling")
        assert resp.message == "needs error handling"

    def test_service_respond_passes_message(self):
        app_state = MagicMock()
        app_state.pending_approval.return_value = None
        svc = ApprovalService(app_state)
        svc.respond(allowed=True, message="add tests please")
        assert svc._response is not None
        assert svc._response.message == "add tests please"
        assert svc._response.allowed is True

    def test_service_respond_default_message_empty(self):
        app_state = MagicMock()
        app_state.pending_approval.return_value = None
        svc = ApprovalService(app_state)
        svc.respond(allowed=True)
        assert svc._response.message == ""


# ── PlanApprovalOverlay ──────────────────────────────────────────────────────


def _text(ov: PlanApprovalOverlay, width: int = 80) -> str:
    console = Console(record=True, width=width, color_system=None, highlight=False)
    console.print(ov.render())
    return console.export_text()


def _terminal(monkeypatch: pytest.MonkeyPatch, cols: int = 80, rows: int = 40) -> None:
    monkeypatch.setattr(
        "agenthicc.tui.workspace.dialog.shutil.get_terminal_size",
        lambda _fallback=(80, 24): os.terminal_size((cols, rows)),
    )


def _type(ov: PlanApprovalOverlay, text: str) -> None:
    for ch in text:
        ov.handle_key(Key.CHAR, ch)


class TestPlanApprovalSelecting:
    def test_renders_through_dialog(self):
        ov, _, _ = _make_overlay()
        assert isinstance(ov.dialog, Dialog)
        assert isinstance(ov.render(), Group)
        assert "Approve the plan" in _text(ov)

    def test_down_cycles_options(self):
        ov, _, _ = _make_overlay()
        labels = [option.label for option in ov.dialog.options]
        assert labels == ["Approve", "Keep planning", "Reject, with feedback"]
        for expected in (1, 2, 0):
            ov.handle_key(Key.DOWN, "")
            assert ov.dialog.selected == expected

    def test_up_wraps(self):
        ov, _, _ = _make_overlay()
        ov.handle_key(Key.UP, "")
        assert ov.dialog.selected == len(ov.dialog.options) - 1

    def test_esc_keeps_the_plan_pending_and_enter_reopens(self):
        ov, service, close_fn = _make_overlay()
        ov.handle_key(Key.ESC, "")
        assert ov.folded
        assert "waiting for your review" in _text(ov)
        service.respond.assert_not_called()
        close_fn.assert_not_called()
        ov.handle_key(Key.CHAR, "1")  # ignored while folded
        service.respond.assert_not_called()
        ov.handle_key(Key.ENTER, "")
        assert not ov.folded

    def test_enter_approves(self):
        ov, service, close_fn = _make_overlay()
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=True, message="")
        close_fn.assert_called_once()

    def test_keep_planning_is_not_a_rejection_with_feedback(self):
        ov, service, close_fn = _make_overlay()
        ov.handle_key(Key.CHAR, "2")
        service.respond.assert_called_once_with(allowed=False, message=KEEP_PLANNING_MESSAGE)
        close_fn.assert_called_once()

    def test_overlay_always_consumes_keys(self):
        ov, _, _ = _make_overlay()
        assert ov.handle_key(Key.CHAR, "x") is True
        assert ov.handle_key(Key.DOWN, "") is True

    def test_plan_content_in_render(self):
        ov, _, _ = _make_overlay("My important plan step")
        assert "My important plan step" in _text(ov)

    def test_no_emoji_or_old_wording(self):
        ov, _, _ = _make_mode_overlay()
        text = _text(ov)
        assert "Plan Review" not in text and "Approve - Safe" not in text
        assert "📋" not in text

    def test_three_line_plan_shrinks_to_twelve_rows(self, monkeypatch):
        _terminal(monkeypatch, rows=50)
        ov, _, _ = _make_mode_overlay("1. one\n2. two\n3. three")
        rendered = ov.render()
        assert isinstance(rendered, Group)
        assert len(rendered.renderables) <= 12
        text = _text(ov)
        assert "one" in text and "three" in text

    def test_long_plan_uses_rows_minus_six_and_scrolls(self, monkeypatch):
        _terminal(monkeypatch, rows=40)
        plan = "\n".join(f"- step {i}" for i in range(200))
        ov, _, _ = _make_mode_overlay(plan)
        rendered = ov.render()
        assert isinstance(rendered, Group)
        assert len(rendered.renderables) == 40 - 6
        text = _text(ov)
        assert "step 0" in text and "more" in text and "step 199" not in text
        for _ in range(300):
            ov.handle_key(Key.PAGE_DOWN, "")
        assert "step 199" in _text(ov)
        ov.handle_key(Key.CHAR, "[")
        assert ov.dialog.scroll > 0

    def test_on_mount_resets_scroll(self):
        ov, _, _ = _make_overlay("\n\n".join(f"Step {i}" for i in range(80)))
        ov.render()
        ov.handle_key(Key.CHAR, "]")
        ov.on_mount()
        assert ov.dialog.scroll == 0


class TestPlanApprovalFeedback:
    def test_reject_with_feedback_uses_the_embedded_composer(self):
        from agenthicc.tui.input.embedded import EmbeddedComposer

        ov, service, close_fn = _make_overlay()
        ov.handle_key(Key.CHAR, "3")
        assert isinstance(ov._composer, EmbeddedComposer)
        assert "What should change?" in _text(ov)
        _type(ov, "needs error handling")
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=False, message="needs error handling")
        close_fn.assert_called_once()

    def test_esc_in_the_composer_returns_to_the_options(self):
        ov, service, close_fn = _make_overlay()
        ov.handle_key(Key.CHAR, "3")
        _type(ov, "some text")
        ov.handle_key(Key.ESC, "")
        assert ov._composer is None
        assert not ov.folded
        service.respond.assert_not_called()
        close_fn.assert_not_called()

    def test_tab_adds_a_note_to_an_approval(self):
        ov, service, close_fn = _make_mode_overlay()
        ov.handle_key(Key.DOWN, "")  # Approve and run in Yolo mode
        ov.handle_key(Key.TAB, "")
        _type(ov, "also add tests")
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=True, message="also add tests", mode="Yolo")
        close_fn.assert_called_once()


class TestPlanApprovalExecutionModes:
    def test_the_four_options(self):
        ov, _, _ = _make_mode_overlay()
        assert [option.label for option in ov.dialog.options] == [
            "Approve and run in Safe mode",
            "Approve and run in Yolo mode",
            "Keep planning",
            "Reject, with feedback",
        ]
        text = _text(ov)
        assert "Approve and run in Safe mode" in text

    def test_safe_approval_returns_safe_mode(self):
        ov, service, close_fn = _make_mode_overlay()
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=True, message="", mode="Safe")
        close_fn.assert_called_once()

    def test_yolo_approval_returns_yolo_mode(self):
        ov, service, close_fn = _make_mode_overlay()
        ov.handle_key(Key.CHAR, "2")
        service.respond.assert_called_once_with(allowed=True, message="", mode="Yolo")
        close_fn.assert_called_once()
