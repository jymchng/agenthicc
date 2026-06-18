"""Unit tests: PlanApprovalOverlay, PromptOverlay base, ApprovalRequest/Response changes."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.prompt import PromptOverlay
from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay, _OPTIONS
from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse, ApprovalService

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_req(plan: str = "Step 1\nStep 2\nStep 3") -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="Review: plan",
        tool_use_id="abc",
        tool_input={"plan": plan},
        capabilities=frozenset(),
        event=asyncio.Event(),
        kind="plan_review",
    )


def _make_overlay(plan: str = "Step 1\nStep 2") -> tuple[PlanApprovalOverlay, MagicMock, MagicMock]:
    req      = _make_req(plan)
    service  = MagicMock()
    close_fn = MagicMock()
    ov = PlanApprovalOverlay(req, service, close_fn)
    ov.on_mount()
    return ov, service, close_fn


# ── ApprovalRequest / ApprovalResponse data-model ────────────────────────────

class TestDataModel:
    def test_request_default_kind_is_tool(self):
        req = ApprovalRequest(
            tool_name="write_file", tool_use_id="x",
            tool_input={}, capabilities=frozenset(),
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


# ── PromptOverlay base ────────────────────────────────────────────────────────

class _ConcretePrompt(PromptOverlay):
    """Minimal concrete subclass for testing the base."""
    name = "test_prompt"
    def render(self): return None
    def handle_key(self, key, ch): return True


class TestPromptOverlay:
    def test_initial_text_empty(self):
        ov = _ConcretePrompt()
        ov.on_mount()
        assert ov._prompt_text == ""

    def test_char_inserts(self):
        ov = _ConcretePrompt()
        ov.on_mount()
        ov._handle_prompt_key(Key.CHAR, "h")
        ov._handle_prompt_key(Key.CHAR, "i")
        assert ov._prompt_text == "hi"

    def test_backspace_deletes(self):
        ov = _ConcretePrompt()
        ov.on_mount()
        for ch in "hello":
            ov._handle_prompt_key(Key.CHAR, ch)
        ov._handle_prompt_key(Key.BACKSPACE, "")
        assert ov._prompt_text == "hell"

    def test_newline_not_inserted(self):
        ov = _ConcretePrompt()
        ov.on_mount()
        ov._handle_prompt_key(Key.CHAR, "\n")
        assert ov._prompt_text == ""

    def test_unknown_key_returns_false(self):
        ov = _ConcretePrompt()
        ov.on_mount()
        consumed = ov._handle_prompt_key(Key.ENTER, "")
        assert consumed is False

    def test_mount_clears_buffer(self):
        ov = _ConcretePrompt()
        ov.on_mount()
        for ch in "hello":
            ov._handle_prompt_key(Key.CHAR, ch)
        ov.on_mount()   # remount should clear
        assert ov._prompt_text == ""


# ── PlanApprovalOverlay — SELECTING state ────────────────────────────────────

class TestPlanApprovalSelecting:
    def test_initial_state_is_selecting(self):
        ov, _, _ = _make_overlay()
        from agenthicc.tui.workspace.overlays.plan_approval import _State
        assert ov._state == _State.SELECTING

    def test_down_cycles_options(self):
        ov, _, _ = _make_overlay()
        assert ov._selected == 0
        ov.handle_key(Key.DOWN, "")
        assert ov._selected == 1
        ov.handle_key(Key.DOWN, "")
        assert ov._selected == 2
        ov.handle_key(Key.DOWN, "")
        assert ov._selected == 0  # wraps

    def test_up_cycles_options(self):
        ov, _, _ = _make_overlay()
        ov.handle_key(Key.UP, "")
        assert ov._selected == len(_OPTIONS) - 1

    def test_esc_in_selecting_calls_respond_denied(self):
        ov, service, close_fn = _make_overlay()
        ov.handle_key(Key.ESC, "")
        service.respond.assert_called_once_with(allowed=False, message="")
        close_fn.assert_called_once()

    def test_enter_option0_approves_immediately(self):
        ov, service, close_fn = _make_overlay()
        ov._selected = 0
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=True, message="")
        close_fn.assert_called_once()

    def test_enter_option1_enters_prompting(self):
        from agenthicc.tui.workspace.overlays.plan_approval import _State
        ov, service, close_fn = _make_overlay()
        ov._selected = 1
        ov.handle_key(Key.ENTER, "")
        assert ov._state == _State.PROMPTING
        service.respond.assert_not_called()
        close_fn.assert_not_called()

    def test_enter_option2_enters_prompting(self):
        from agenthicc.tui.workspace.overlays.plan_approval import _State
        ov, service, close_fn = _make_overlay()
        ov._selected = 2
        ov.handle_key(Key.ENTER, "")
        assert ov._state == _State.PROMPTING

    def test_overlay_always_consumes_keys(self):
        ov, _, _ = _make_overlay()
        assert ov.handle_key(Key.CHAR, "x") is True
        assert ov.handle_key(Key.DOWN, "") is True
        assert ov.handle_key(Key.ENTER, "") is True

    def test_render_selecting_returns_group(self):
        from rich.console import Group
        ov, _, _ = _make_overlay("Step 1\nStep 2")
        result = ov.render()
        assert isinstance(result, Group)

    def test_plan_content_in_render(self):
        from rich.console import Console
        ov, _, _ = _make_overlay("My important plan step")
        result = ov.render()
        console = Console(width=80, highlight=False)
        with console.capture() as cap:
            console.print(result)
        assert "My important plan step" in cap.get()

    def test_plan_scroll_indicator_shown(self):
        # Double-newline separation → distinct Markdown paragraphs → many
        # rendered lines after expansion (each gets its own line + spacer,
        # so 25 paragraphs ≫ _PLAN_VISIBLE_LINES = 20).
        long_plan = "\n\n".join(f"Step {i}" for i in range(25))
        ov, _, _ = _make_overlay(long_plan)
        result = ov.render()
        combined = " ".join(
            r.plain if hasattr(r, "plain") else str(r)
            for r in result.renderables
        )
        assert "lines" in combined and "of " in combined

    def test_scroll_down_with_bracket(self):
        long_plan = "\n\n".join(f"Step {i}" for i in range(25))
        ov, _, _ = _make_overlay(long_plan)
        ov.render()   # populates _rendered_lines and sets _plan_visible
        if len(ov._rendered_lines) > ov._plan_visible:
            assert ov._plan_scroll == 0
            ov.handle_key(Key.CHAR, "]")
            assert ov._plan_scroll == 1

    def test_scroll_up_with_bracket(self):
        long_plan = "\n".join(f"Step {i}" for i in range(20))
        ov, _, _ = _make_overlay(long_plan)
        ov._plan_scroll = 5
        ov.handle_key(Key.CHAR, "[")
        assert ov._plan_scroll == 4

    def test_scroll_clamped_at_top(self):
        long_plan = "\n".join(f"Step {i}" for i in range(20))
        ov, _, _ = _make_overlay(long_plan)
        ov._plan_scroll = 0
        ov.handle_key(Key.CHAR, "[")
        assert ov._plan_scroll == 0   # cannot go below 0

    def test_scroll_clamped_at_bottom(self):
        long_plan = "\n\n".join(f"Step {i}" for i in range(25))
        ov, _, _ = _make_overlay(long_plan)
        ov.render()   # populate _rendered_lines and set _plan_visible
        total      = len(ov._rendered_lines)
        max_scroll = max(0, total - ov._plan_visible)
        ov._plan_scroll = max_scroll
        ov.handle_key(Key.CHAR, "]")
        assert ov._plan_scroll == max_scroll  # cannot go past max

    def test_short_plan_no_scroll_indicator(self):
        # 3 lines ≤ _PLAN_VISIBLE_LINES → no scroll indicator
        short_plan = "Line 1\nLine 2\nLine 3"
        ov, _, _ = _make_overlay(short_plan)
        result = ov.render()
        combined = " ".join(
            r.plain if hasattr(r, "plain") else str(r)
            for r in result.renderables
        )
        assert "of 3" not in combined   # no indicator for short plans

    def test_on_mount_resets_scroll(self):
        long_plan = "\n".join(f"Step {i}" for i in range(20))
        ov, _, _ = _make_overlay(long_plan)
        ov._plan_scroll = 7
        ov.on_mount()
        assert ov._plan_scroll == 0


# ── PlanApprovalOverlay — PROMPTING state ────────────────────────────────────

class TestPlanApprovalPrompting:
    def _enter_prompting(self, option: int) -> tuple[PlanApprovalOverlay, MagicMock, MagicMock]:
        ov, service, close_fn = _make_overlay()
        ov._selected = option
        ov.handle_key(Key.ENTER, "")
        return ov, service, close_fn

    def test_enter_option1_then_type_then_submit(self):
        ov, service, close_fn = self._enter_prompting(1)
        for ch in "needs error handling":
            ov.handle_key(Key.CHAR, ch)
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=False, message="needs error handling")
        close_fn.assert_called_once()

    def test_enter_option2_then_type_then_submit(self):
        ov, service, close_fn = self._enter_prompting(2)
        for ch in "also add tests":
            ov.handle_key(Key.CHAR, ch)
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=True, message="also add tests")

    def test_esc_in_prompting_returns_to_selecting(self):
        from agenthicc.tui.workspace.overlays.plan_approval import _State
        ov, service, close_fn = self._enter_prompting(1)
        ov.handle_key(Key.ESC, "")
        assert ov._state == _State.SELECTING
        service.respond.assert_not_called()
        close_fn.assert_not_called()

    def test_esc_clears_buffer(self):
        ov, _, _ = self._enter_prompting(1)
        for ch in "some text":
            ov.handle_key(Key.CHAR, ch)
        ov.handle_key(Key.ESC, "")
        assert ov._prompt_text == ""

    def test_render_prompting_returns_group(self):
        from rich.console import Group
        ov, _, _ = self._enter_prompting(1)
        result = ov.render()
        assert isinstance(result, Group)

    def test_render_prompting_shows_option_label(self):
        ov, _, _ = self._enter_prompting(1)
        result = ov.render()
        combined = " ".join(
            r.plain if hasattr(r, "plain") else str(r)
            for r in result.renderables
        )
        assert "Reject" in combined

    def test_submit_empty_prompt_allowed(self):
        ov, service, close_fn = self._enter_prompting(1)
        ov.handle_key(Key.ENTER, "")
        service.respond.assert_called_once_with(allowed=False, message="")
