"""TUX-10 §2.5 / AC 6: paths and URLs in the transcript are OSC 8 hyperlinks.

One helper (``tui/terminal/hyperlinks.py``) serves the tool rows, the diffs,
the notices, the assistant's text and the transcript view.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from agenthicc.config import UISettings
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.terminal.hyperlinks import (
    Hyperlinks,
    autolink_urls,
    links_enabled,
)
from agenthicc.tui.terminal.osc import hyperlinks_supported
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.unit

_LINK = re.compile(r"\x1b\]8;[^;]*;([^\x1b]*)\x1b\\")


def _links_in(output: str) -> list[str]:
    return [target for target in _LINK.findall(output) if target]


def _render(
    tmp_path: Path,
    *events: tuple[str, dict[str, object]],
    ui: UISettings,
    terminal: bool = True,
) -> str:
    state = AppState.create()
    state.ui.set(ui)
    state.conversation.current_directory.set(str(tmp_path))
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=terminal,
        color_system="standard" if terminal else None,
        width=100,
    )
    appender = ScrollBufferAppender(state, console)
    for index, (kind, payload) in enumerate(events):
        appender._render_one(ConversationEvent(f"e{index}", kind, payload))
    return stream.getvalue()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    return tmp_path


def _read(path: str) -> tuple[str, dict[str, object]]:
    return (
        "tool_complete",
        {"name": "read_file", "success": True, "summary": path, "state": "exited"},
    )


# ── AC 6 ──────────────────────────────────────────────────────────────────────


def test_a_read_row_links_to_the_file_when_hyperlinks_are_on(workspace: Path) -> None:
    output = _render(workspace, _read("src/app.py"), ui=UISettings(hyperlinks="on"))
    assert _links_in(output) == [f"file://{workspace}/src/app.py"]
    assert "src/app.py" in output


def test_the_row_has_no_link_when_hyperlinks_are_off(workspace: Path) -> None:
    output = _render(workspace, _read("src/app.py"), ui=UISettings(hyperlinks="off"))
    assert "\x1b]8;" not in output and "src/app.py" in output


def test_auto_links_nothing_where_the_terminal_is_not_known_to_open_them(workspace: Path) -> None:
    """Under pytest stdout is not a terminal, so ``auto`` is off."""
    output = _render(workspace, _read("src/app.py"), ui=UISettings(hyperlinks="auto"))
    assert "\x1b]8;" not in output


def test_a_screen_reader_never_gets_links(workspace: Path) -> None:
    output = _render(
        workspace, _read("src/app.py"), ui=UISettings(hyperlinks="on", screen_reader=True)
    )
    assert "\x1b]8;" not in output


def test_a_stream_that_is_not_a_terminal_gets_no_escape_even_when_on(workspace: Path) -> None:
    output = _render(workspace, _read("src/app.py"), ui=UISettings(hyperlinks="on"), terminal=False)
    assert "\x1b" not in output


def test_a_path_that_is_not_there_or_not_a_path_has_no_link(workspace: Path) -> None:
    output = _render(
        workspace,
        _read("src/gone.py"),
        _read("3 files"),
        ui=UISettings(hyperlinks="on"),
    )
    assert _links_in(output) == []


def test_an_editor_scheme_carries_the_line_number() -> None:
    root = Path("/work")
    for editor, expected in (
        ("vscode", "vscode://file/work/src/app.py:12"),
        ("cursor", "cursor://file/work/src/app.py:12"),
        ("zed", "zed://file/work/src/app.py:12"),
        ("idea", "idea://open?file=/work/src/app.py&line=12"),
        ("", "file:///work/src/app.py"),
    ):
        assert Hyperlinks(True, root, editor).url("src/app.py", line=12) == expected
    assert Hyperlinks(True, root, "vscode").url("src/app.py") == "vscode://file/work/src/app.py:1"
    assert Hyperlinks(False, root).url("src/app.py") is None


def test_a_row_links_with_the_editor_scheme_when_one_is_configured(workspace: Path) -> None:
    output = _render(
        workspace, _read("src/app.py"), ui=UISettings(hyperlinks="on", editor_links="vscode")
    )
    assert _links_in(output) == [f"vscode://file{workspace}/src/app.py:1"]


def test_paths_with_spaces_and_unicode_are_percent_encoded(tmp_path: Path) -> None:
    folder = tmp_path / "my docs"
    folder.mkdir()
    (folder / "résumé.md").write_text("x", encoding="utf-8")
    url = Hyperlinks(True, tmp_path).url("my docs/résumé.md")
    assert url is not None and " " not in url and url.startswith("file://")


# ── diffs, notices, text ──────────────────────────────────────────────────────


def test_a_diff_header_links_its_path(workspace: Path) -> None:
    edit = (
        "file_modified",
        {"path": "src/app.py", "old_lines": ["a"], "new_lines": ["b"], "tool": "patch_file"},
    )
    create = (
        "file_modified",
        {"path": "README.md", "old_lines": [], "new_lines": ["# new"], "tool": "write_file"},
    )
    output = _render(workspace, edit, create, ui=UISettings(hyperlinks="on"))
    assert _links_in(output) == [f"file://{workspace}/src/app.py", f"file://{workspace}/README.md"]
    assert "\x1b]8;" not in _render(workspace, edit, create, ui=UISettings(hyperlinks="off"))


def test_a_notice_links_the_files_and_urls_it_names(workspace: Path) -> None:
    error = (
        "error",
        {
            "kind": "internal",
            "message": "Could not read src/app.py:7",
            "detail": "see https://example.org/help, then retry",
        },
    )
    output = _render(workspace, error, ui=UISettings(hyperlinks="on"))
    assert f"file://{workspace}/src/app.py" in _links_in(output)
    assert "https://example.org/help" in _links_in(output)
    assert "\x1b]8;" not in _render(workspace, error, ui=UISettings(hyperlinks="off"))


def test_a_word_that_only_looks_like_a_file_is_not_linked(workspace: Path) -> None:
    text = Text("Use e.g. v1.2 or missing/file.py or src/app.py")
    Hyperlinks(True, workspace).linkify(text)
    linked = [text.plain[span.start : span.end] for span in text.spans if span.style]
    assert linked == ["src/app.py"]
    assert Hyperlinks(False, workspace).linkify(Text("src/app.py")).spans == []


def test_urls_in_the_assistants_text_are_linked(workspace: Path) -> None:
    text = (
        "text",
        {"text": "See https://example.org/docs. And [the guide](https://example.org/guide)."},
    )
    output = _render(workspace, text, ui=UISettings(hyperlinks="on"))
    assert _links_in(output) == ["https://example.org/docs", "https://example.org/guide"]
    off = _render(workspace, text, ui=UISettings(hyperlinks="off"))
    assert "\x1b]8;" not in off
    plain = re.sub(r"\x1b\[[0-9;]*m", "", off)
    assert "the guide (https://example.org/guide)" in plain  # the address is not hidden


def test_autolinking_leaves_code_and_existing_links_alone() -> None:
    source = "a https://x.org b `https://y.org` [t](https://z.org) <https://w.org>\n```\nhttps://q.org\n```"
    assert autolink_urls(source) == source.replace("a https://x.org b", "a <https://x.org> b")


def test_the_transcript_views_path_label_is_linked(workspace: Path) -> None:
    from agenthicc.tui.transcript.render import path_label

    target = str(workspace / "README.md")
    on = path_label(target, Hyperlinks(True, workspace))
    assert [span.style.link for span in on.spans if getattr(span.style, "link", None)] == [
        f"file://{workspace}/README.md"
    ]
    assert not [span for span in path_label(target).spans if getattr(span.style, "link", None)]


def test_the_transcript_view_renders_tool_rows_with_their_links(workspace: Path) -> None:
    """The view reuses the scrollback renderers, so its rows carry the same links."""
    from agenthicc.tui.transcript.render import EventRenderer

    renderer = EventRenderer(UISettings(hyperlinks="on"))
    renderer._state.conversation.current_directory.set(str(workspace))
    event = ConversationEvent("e", *_read("src/app.py"))
    rows = renderer.rows(event, 100, expanded=False)
    links = {
        span.style.link for row in rows for span in row.spans if getattr(span.style, "link", None)
    }
    assert links == {f"file://{workspace}/src/app.py"}


# ── which terminals ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "iTerm.app"}, True),
        ({"TERM": "xterm-kitty"}, True),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "WezTerm"}, True),
        ({"TERM": "xterm-256color", "WT_SESSION": "x"}, True),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "vscode"}, True),
        ({"TERM": "xterm-256color", "VTE_VERSION": "7200"}, True),
        ({"TERM": "xterm-256color", "VTE_VERSION": "4000"}, False),
        ({"TERM": "xterm-256color"}, False),
        ({"TERM": "dumb", "NO_COLOR": "1"}, False),
        ({"TERM": "dumb", "TERM_PROGRAM": "iTerm.app"}, False),
        ({"TERM": "screen", "TERM_PROGRAM": "iTerm.app", "STY": "1"}, False),
        ({"TERM": "tmux-256color", "TMUX": "/t", "TERM_PROGRAM_VERSION": "3.3a"}, False),
        ({"TERM": "tmux-256color", "TMUX": "/t", "TERM_PROGRAM_VERSION": "3.4"}, True),
        ({"TERM": "tmux-256color", "TMUX": "/t", "TERM_PROGRAM_VERSION": "next-3.5"}, True),
        ({"TERM": "tmux-256color", "TMUX": "/t"}, False),
        ({"TERM": "xterm-256color", "FORCE_HYPERLINK": "1"}, True),
        ({"TERM": "xterm-256color", "TERM_PROGRAM": "iTerm.app", "FORCE_HYPERLINK": "0"}, False),
    ],
)
def test_known_terminals_open_links_and_tmux_before_3_4_does_not(
    env: dict[str, str], expected: bool
) -> None:
    assert hyperlinks_supported(env) is expected


def test_the_setting_decides_over_the_terminal() -> None:
    assert links_enabled("on", supported=False) is True
    assert links_enabled("off", supported=True) is False
    assert links_enabled("auto", supported=True) is True
    assert links_enabled("auto", supported=False) is False
    assert links_enabled("on", supported=True, screen_reader=True) is False


def test_the_ui_settings_are_validated_and_parsed(tmp_path: Path) -> None:
    from agenthicc.config import load_config

    config = tmp_path / "agenthicc.toml"
    config.write_text(
        '[ui]\nnotify = "BELL"\nwindow_title = false\nhyperlinks = "On"\neditor_links = "Cursor"\n',
        encoding="utf-8",
    )
    ui = load_config(config_path=str(config)).ui
    assert (ui.notify, ui.window_title, ui.hyperlinks, ui.editor_links) == (
        "bell",
        False,
        "on",
        "cursor",
    )
    defaults = UISettings()
    assert (defaults.notify, defaults.window_title, defaults.hyperlinks, defaults.editor_links) == (
        "auto",
        True,
        "auto",
        "",
    )
    for bad, name in (
        ({"notify": "loud"}, "ui.notify"),
        ({"hyperlinks": "yes"}, "ui.hyperlinks"),
        ({"editor_links": "emacs"}, "ui.editor_links"),
    ):
        with pytest.raises(ValueError, match=name):
            UISettings(**bad)
