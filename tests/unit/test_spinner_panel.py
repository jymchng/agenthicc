"""Unit tests for SpinnerPanel widget (PRD-55 Phase 4).

Tests:
1. test_spinner_panel_add_tool_call       — add a tool call, render contains name
2. test_spinner_panel_running_shows_ellipsis — running tool shows [dim]…[/dim]
3. test_spinner_panel_success_shows_check — done=True/ok=True, render has "✓"
4. test_spinner_panel_failure_shows_cross — done=True/ok=False, render has "✗"
5. test_spinner_panel_diff_preview        — update with diff text, first diff lines appear
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from agenthicc.tui.widgets.spinner_panel import SpinnerPanel

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────────────


class _SpinnerApp(App):
    """Minimal Textual app hosting a single SpinnerPanel for testing."""

    def compose(self) -> ComposeResult:
        yield SpinnerPanel(id="sp")


def _make_panel() -> SpinnerPanel:
    """Create a bare SpinnerPanel (no Textual event loop) for pure-logic tests."""
    return SpinnerPanel()


# ── pure render tests (no event loop needed) ─────────────────────────────────


class TestSpinnerPanelRenderLogic:
    """Test the render() method directly without mounting inside a Textual app."""

    def test_spinner_panel_add_tool_call(self) -> None:
        """After add_tool_call the rendered output must contain the tool name."""
        panel = _make_panel()
        panel.add_tool_call("tid-1", "read_file", "'src/main.py'")
        rendered = panel.render()
        assert "read_file" in rendered

    def test_spinner_panel_running_shows_ellipsis(self) -> None:
        """A running (not-done) tool call must render with the ellipsis marker."""
        panel = _make_panel()
        panel.add_tool_call("tid-2", "list_directory", "'.'")
        rendered = panel.render()
        # The running state uses [dim]…[/dim]
        assert "[dim]…[/dim]" in rendered

    def test_spinner_panel_success_shows_check(self) -> None:
        """After update_tool_call(done=True, ok=True) the render must contain '✓'."""
        panel = _make_panel()
        panel.add_tool_call("tid-3", "write_file", "'out.txt'")
        panel.update_tool_call("tid-3", done=True, ok=True, ms=42.0)
        rendered = panel.render()
        assert "✓" in rendered
        # Ellipsis must no longer appear for this entry.
        assert "[dim]…[/dim]" not in rendered

    def test_spinner_panel_failure_shows_cross(self) -> None:
        """After update_tool_call(done=True, ok=False) the render must contain '✗'."""
        panel = _make_panel()
        panel.add_tool_call("tid-4", "run_bash", "'bad_cmd'")
        panel.update_tool_call("tid-4", done=True, ok=False, ms=10.0)
        rendered = panel.render()
        assert "✗" in rendered
        assert "✓" not in rendered

    def test_spinner_panel_diff_preview(self) -> None:
        """When diff text is provided, the first diff lines must appear in render()."""
        panel = _make_panel()
        panel.add_tool_call("tid-5", "patch_file", "'app.py'")
        diff_text = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,3 +1,4 @@\n"
            " existing line\n"
            "+new added line\n"
            "-old removed line\n"
            " context line\n"
        )
        panel.update_tool_call("tid-5", done=True, ok=True, ms=5.0, diff=diff_text)
        rendered = panel.render()
        # Header lines (---/+++) are shown dim.
        assert "--- a/app.py" in rendered
        # Added lines are shown in green.
        assert "+new added line" in rendered
        # Removed lines are shown in red.
        assert "-old removed line" in rendered

    def test_spinner_panel_diff_truncated(self) -> None:
        """When diff has more than 8 lines, a 'more diff lines' note is added."""
        panel = _make_panel()
        panel.add_tool_call("tid-6", "write_file", "'big.py'")
        # Build a diff with 12 change lines (> 8).
        diff_lines = ["--- a/big.py\n", "+++ b/big.py\n"] + [
            f"+line {i}\n" for i in range(10)
        ]
        diff_text = "".join(diff_lines)
        panel.update_tool_call("tid-6", done=True, ok=True, ms=1.0, diff=diff_text)
        rendered = panel.render()
        assert "more diff lines" in rendered

    def test_spinner_panel_ms_displayed(self) -> None:
        """Duration in milliseconds must appear in the rendered output."""
        panel = _make_panel()
        panel.add_tool_call("tid-7", "git_status", "")
        panel.update_tool_call("tid-7", done=True, ok=True, ms=123.0)
        rendered = panel.render()
        assert "123" in rendered

    def test_spinner_panel_hide_clears_calls(self) -> None:
        """hide() must clear all tracked tool calls."""
        panel = _make_panel()
        panel.add_tool_call("tid-8", "read_file", "'x.py'")
        assert panel._tool_calls
        panel.hide()
        assert not panel._tool_calls
        assert panel.render() == ""

    def test_spinner_panel_multiple_calls(self) -> None:
        """Multiple tool calls are all shown in render output."""
        panel = _make_panel()
        panel.add_tool_call("a", "read_file", "'a.py'")
        panel.add_tool_call("b", "write_file", "'b.py'")
        rendered = panel.render()
        assert "read_file" in rendered
        assert "write_file" in rendered

    def test_spinner_panel_unknown_id_update_is_noop(self) -> None:
        """Updating a non-existent tool_use_id must not raise."""
        panel = _make_panel()
        panel.update_tool_call("nonexistent", done=True, ok=True, ms=None)  # no error


# ── Textual integration tests (event loop required) ───────────────────────────


@pytest.mark.asyncio
async def test_spinner_panel_hidden_by_default() -> None:
    """SpinnerPanel must start with display:none (no 'active' CSS class)."""
    app = _SpinnerApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)
        assert "active" not in sp.classes


@pytest.mark.asyncio
async def test_spinner_panel_show_activates() -> None:
    """Calling show() must add the 'active' CSS class."""
    app = _SpinnerApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)
        sp.show()
        await pilot.pause()
        assert "active" in sp.classes


@pytest.mark.asyncio
async def test_spinner_panel_hide_deactivates() -> None:
    """Calling hide() after show() must remove the 'active' CSS class."""
    app = _SpinnerApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)
        sp.show()
        await pilot.pause()
        sp.hide()
        await pilot.pause()
        assert "active" not in sp.classes


@pytest.mark.asyncio
async def test_spinner_panel_tool_call_started_message() -> None:
    """ToolCallStarted message posted on the panel must register the tool call."""
    from agenthicc.tui.messages import ToolCallStarted

    app = _SpinnerApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)
        sp.post_message(ToolCallStarted("tid-msg", "read_file", {"path": "src/main.py"}))
        await pilot.pause()
        await pilot.pause()
        assert "tid-msg" in sp._tool_calls
        rendered = sp.render()
        assert "read_file" in rendered


@pytest.mark.asyncio
async def test_spinner_panel_tool_call_complete_message() -> None:
    """ToolCallComplete message posted on the panel must mark the call done."""
    from agenthicc.tui.messages import ToolCallComplete, ToolCallStarted

    app = _SpinnerApp()
    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        sp = app.query_one("#sp", SpinnerPanel)
        sp.post_message(ToolCallStarted("tid-c", "run_bash", {"cmd": "ls"}))
        await pilot.pause()
        await pilot.pause()
        sp.post_message(ToolCallComplete("tid-c", success=True, duration_ms=55.0, error=None, diff=None))
        await pilot.pause()
        await pilot.pause()
        entry = sp._tool_calls.get("tid-c")
        assert entry is not None
        assert entry["done"] is True
        assert entry["ok"] is True
        rendered = sp.render()
        assert "✓" in rendered
