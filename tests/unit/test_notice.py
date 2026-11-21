"""TUX-08 §2.2: one notice component, and every scrollback notice renderer uses it."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from rich.console import Console, RenderableType

from agenthicc.config import UISettings
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, current_theme, palette, use_theme
from agenthicc.tui.workspace.appender import ScrollBufferAppender
from agenthicc.tui.workspace.notice import Notice

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _dark_theme() -> Iterator[None]:
    before = current_theme()
    use_theme(BUILTIN_THEMES["dark"])
    yield
    use_theme(before)


def _text(renderable: RenderableType, width: int = 80) -> str:
    console = Console(record=True, width=width, force_terminal=False, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_each_severity_has_its_glyph_and_token() -> None:
    console = Console(record=True, width=80, force_terminal=True, color_system="truecolor")
    for severity, glyph, token in (
        ("error", "✗", "danger"),
        ("warning", "!", "warning"),
        ("info", "·", "muted"),
        ("success", "✓", "success"),
    ):
        console.print(Notice(severity, "Title"))
        segments = list(console.render(Notice(severity, "Title")))
        marker = next(segment for segment in segments if segment.text == glyph)
        assert marker.style is not None and marker.style.color is not None
        assert marker.style.color.name == getattr(palette(), token)
    assert console.export_text().splitlines() == [" ✗ Title", " ! Title", " · Title", " ✓ Title"]


def test_the_title_wraps_under_itself_and_the_body_follows() -> None:
    notice = Notice(
        "error",
        "No API key for anthropic, and this title is long enough to wrap",
        "Set ANTHROPIC_API_KEY, or run /model to switch provider.",
    )
    assert _text(notice, width=40).splitlines() == [
        " ✗ No API key for anthropic, and this",
        "   title is long enough to wrap",
        "   Set ANTHROPIC_API_KEY, or run /model",
        "   to switch provider.",
    ]


def test_actions_follow_the_title() -> None:
    notice = Notice("warning", 'MCP server "github" is unavailable.', actions=("/mcp",))
    assert _text(notice).splitlines() == [' ! MCP server "github" is unavailable.   /mcp']
    assert notice.plain() == '! MCP server "github" is unavailable.   /mcp'


def test_screen_readers_get_words_and_ascii_glyphs_stay_ascii() -> None:
    assert _text(Notice("error", "Boom", words=True)).splitlines() == [" Error: Boom"]
    assert _text(Notice("warning", "Careful", words=True)).splitlines() == [" Warning: Careful"]
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    rendered = _text(Notice("error", "Boom", "body"))
    assert rendered.isascii()
    assert rendered.splitlines() == [" x Boom", "   body"]


def test_a_literal_notice_prints_its_text_exactly() -> None:
    text = "[bold]Approval[/bold] needed: **now**"
    assert _text(Notice("info", text, literal=True)) == text + "\n"


# ── renderer coverage (AC 4) ─────────────────────────────────────────────────

_NOTICE_EVENTS: dict[str, dict[str, object]] = {
    "error": {
        "message": "No API key for anthropic.",
        "kind": "missing_credentials",
        "action": {"hint": "Set ANTHROPIC_API_KEY, or run /model to switch provider."},
    },
    "system": {"text": "Compacted 84 messages"},
    "announcement": {"text": "The approval request is ready."},
    "tool_recovery": {},
    "mcp_server_failure": {"text": "MCP server is unavailable; continuing.", "required": False},
    "network_retry": {"attempt": 1, "max_retries": 5, "delay_s": 1.0, "status_code": 503},
    "provider_recovery_retry": {"attempt": 2, "max_retries": 5, "delay_s": 1.0},
}


class _RecordingConsole(Console):
    def __init__(self) -> None:
        super().__init__(record=True, width=100, force_terminal=False, color_system=None)
        self.printed: list[object] = []

    def print(self, *objects: object, **kwargs: object) -> None:  # type: ignore[override]
        self.printed.extend(objects)
        super().print(*objects, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", sorted(_NOTICE_EVENTS))
def test_every_notice_renderer_prints_only_notices(kind: str) -> None:
    console = _RecordingConsole()
    appender = ScrollBufferAppender(AppState.create(), console)
    appender._render_one(ConversationEvent("e", kind, _NOTICE_EVENTS[kind]))
    content = [item for item in console.printed if item not in ("",)]
    assert content, kind
    assert all(isinstance(item, Notice) for item in content), content


def test_a_turn_failure_renders_the_message_and_its_action() -> None:
    console = _RecordingConsole()
    appender = ScrollBufferAppender(AppState.create(), console)
    appender._render_one(ConversationEvent("e", "error", _NOTICE_EVENTS["error"]))
    assert console.export_text().splitlines() == [
        " ✗ No API key for anthropic.",
        "   Set ANTHROPIC_API_KEY, or run /model to switch provider.",
    ]


def test_an_old_error_without_a_kind_renders_its_stored_message() -> None:
    console = _RecordingConsole()
    appender = ScrollBufferAppender(AppState.create(), console)
    appender._render_one(
        ConversationEvent("e", "error", {"message": "Agent turn failed (TypeError)"})
    )
    assert console.export_text().splitlines() == [" ✗ Agent turn failed (TypeError)"]


def test_retry_notices_keep_attempt_reason_and_detail_in_order() -> None:
    console = _RecordingConsole()
    appender = ScrollBufferAppender(AppState.create(), console)
    appender._render_one(
        ConversationEvent(
            "r",
            "network_retry",
            {
                "attempt": 2,
                "max_retries": 5,
                "delay_s": 1.0,
                "reason": "Temporary network problem",
                "detail": "The connection was interrupted.",
                "status_code": 503,
            },
        )
    )
    appender._render_one(ConversationEvent("p", "provider_recovery_retry", {"attempt": 1}))
    assert console.export_text().splitlines() == [
        " ! Network issue · retry 2/5 in 1.0s",
        "   Temporary network problem · HTTP 503",
        "   The connection was interrupted.",
        "",
        " ! Provider request rejected · retry 1/1 in 0.0s",
        "   provider rejected request",
        "",
    ]


def test_a_required_mcp_failure_is_an_error_and_an_optional_one_a_warning() -> None:
    console = _RecordingConsole()
    appender = ScrollBufferAppender(AppState.create(), console)
    appender._render_one(ConversationEvent("a", "mcp_server_failure", {"required": True}))
    appender._render_one(ConversationEvent("b", "mcp_server_failure", {"required": False}))
    severities = [item.severity for item in console.printed if isinstance(item, Notice)]
    assert severities == ["error", "warning"]


def test_screen_reader_mode_spells_severities_and_keeps_announcements_literal() -> None:
    state = AppState.create()
    state.ui.set(replace(UISettings(), screen_reader=True))
    console = _RecordingConsole()
    appender = ScrollBufferAppender(state, console)
    appender._render_one(ConversationEvent("e", "error", {"message": "Boom"}))
    appender._render_one(ConversationEvent("a", "announcement", {"text": "Ready. Use y."}))
    assert console.export_text().splitlines() == [" Error: Boom", "Ready. Use y."]
