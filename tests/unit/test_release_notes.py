"""TUX-03 §2.2: release notes come from the packaged CHANGELOG.md, and /release-notes pages them."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.commands.builtins import build_builtin_registry
from agenthicc.commands.command import CommandContext
from agenthicc.commands.dispatcher import CommandDispatcher
from agenthicc.config import AgenthiccConfig
from agenthicc.release_notes import (
    ReleaseEntry,
    load_entries,
    parse_changelog,
    select,
    version_key,
)
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.workspace.overlays.release_notes import ReleaseNotesOverlay
from agenthicc.user_state import UserState, load_user_state, save_user_state

pytestmark = pytest.mark.unit

_CHANGELOG = """\
# Changelog

All notable changes are documented here.

---

## [Unreleased]

### Added

- Work that is in this build but in no release yet.

---

## [0.3.0] — 2026-03-01

### Fixed

- Fix number three.

## [0.2.0] — 2026-02-01

- Feature number two.

## [0.1.0] — 2026-01-01

- Feature number one.
"""


def _entries() -> tuple[ReleaseEntry, ...]:
    return parse_changelog(_CHANGELOG)


# ── parsing ───────────────────────────────────────────────────────────────────


def test_the_changelog_is_parsed_into_sections_newest_first() -> None:
    entries = _entries()

    assert [(entry.version, entry.date) for entry in entries] == [
        ("Unreleased", ""),
        ("0.3.0", "2026-03-01"),
        ("0.2.0", "2026-02-01"),
        ("0.1.0", "2026-01-01"),
    ]
    assert entries[0].unreleased
    assert "Fix number three." in entries[1].body
    assert "---" not in entries[0].body


def test_an_empty_section_is_dropped() -> None:
    assert [
        entry.version
        for entry in parse_changelog("## [Unreleased]\n\n## [1.0.0] — 2026-01-01\n\n- x\n")
    ] == ["1.0.0"]


def test_versions_order_numerically() -> None:
    assert version_key("0.10.0") > version_key("0.9.1")
    assert version_key("0+unknown") == (0,)
    assert version_key("") == ()


def test_the_packaged_changelog_has_the_current_notes() -> None:
    entries = load_entries()

    assert entries, "the repository's CHANGELOG.md must parse into sections"
    assert entries[0].unreleased
    assert any(entry.version == "0.1.0" for entry in entries)


# ── what is listed ────────────────────────────────────────────────────────────


def test_the_installed_version_and_unreleased_work_are_listed() -> None:
    chosen = select(_entries(), installed="0.3.0")

    assert [entry.version for entry in chosen] == ["Unreleased", "0.3.0"]


def test_every_version_since_the_last_one_seen_is_listed() -> None:
    chosen = select(_entries(), installed="0.3.0", last_seen="0.1.0")

    assert [entry.version for entry in chosen] == ["Unreleased", "0.3.0", "0.2.0"]


def test_nothing_newer_than_the_installed_version_is_listed() -> None:
    chosen = select(_entries(), installed="0.2.0", last_seen="0.1.0")

    assert [entry.version for entry in chosen] == ["Unreleased", "0.2.0"]


def test_a_build_without_a_section_shows_the_newest_release() -> None:
    chosen = select(_entries(), installed="0+unknown")

    assert [entry.version for entry in chosen] == ["Unreleased", "0.3.0"]


# ── the overlay ───────────────────────────────────────────────────────────────


def _overlay(
    closed: list[bool], *, entries: tuple[ReleaseEntry, ...] | None = None
) -> ReleaseNotesOverlay:
    overlay = ReleaseNotesOverlay(
        entries if entries is not None else select(_entries(), installed="0.3.0"),
        version="0.3.0",
        on_close=lambda: closed.append(True),
    )
    overlay.set_viewport(100, 20)
    return overlay


def _text(overlay: Overlay, width: int = 100) -> str:
    console = Console(width=width, file=io.StringIO(), force_terminal=False, color_system=None)
    console.print(overlay.render())
    return console.file.getvalue()  # type: ignore[attr-defined]


def test_the_overlay_is_a_dialog_with_the_entries_in_its_body() -> None:
    from agenthicc.tui.workspace.dialog import Dialog

    overlay = _overlay([])
    text = _text(overlay)

    assert isinstance(overlay.dialog, Dialog)
    assert "Release notes" in text
    assert "agenthicc 0.3.0" in text
    assert "Unreleased" in text
    assert "Fix number three." in text


def test_a_long_changelog_scrolls_with_the_arrows_and_pages() -> None:
    long_body = "\n".join(f"- change number {n}" for n in range(60))
    overlay = _overlay([], entries=(ReleaseEntry("0.3.0", "2026-03-01", long_body),))

    first = _text(overlay)
    assert "change number 0" in first and "change number 59" not in first
    assert "more" in first  # the ↓ N more marker

    for _ in range(3):
        overlay.handle_key(Key.DOWN, "")
    assert overlay.dialog.scroll == 3
    overlay.handle_key(Key.PAGE_DOWN, "")
    assert overlay.dialog.scroll > 3
    overlay.handle_key(Key.UP, "")
    assert "change number 0" not in _text(overlay)


def test_escape_and_q_close_the_pager() -> None:
    closed: list[bool] = []
    overlay = _overlay(closed)

    overlay.handle_key(Key.DOWN, "")
    assert closed == []
    overlay.handle_key(Key.ESC, "")
    overlay.handle_key(Key.CHAR, "q")

    assert closed == [True, True]


def test_an_empty_changelog_says_so() -> None:
    assert "No release notes ship with this installation." in _text(_overlay([], entries=()))


def test_the_ascii_glyph_set_draws_the_pager_without_box_characters() -> None:
    from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, use_theme

    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))

    assert _text(_overlay([])).isascii()


# ── the command ───────────────────────────────────────────────────────────────


@pytest.fixture
def changelog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(_CHANGELOG, encoding="utf-8")
    monkeypatch.setattr("agenthicc.release_notes.changelog_path", lambda: path)
    monkeypatch.setattr("agenthicc.cli.version.package_version", lambda: "0.3.0")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return path


def _dispatch(app_state: AppState | None = None) -> list[Overlay]:
    shown: list[Overlay] = []
    context = CommandContext(
        text="/release-notes",
        args="",
        model="test/model",
        console=Console(file=io.StringIO()),
        config=AgenthiccConfig(),
        set_pending_menu=shown.append,
        close_overlay=lambda: None,
        app_state=app_state,
    )
    assert CommandDispatcher(build_builtin_registry()).dispatch("/release-notes", context)
    return shown


def test_release_notes_is_a_builtin_command_the_picker_can_see() -> None:
    command = build_builtin_registry().get("/release-notes")

    assert command is not None
    assert command.menu_factory is not None


def test_the_command_opens_the_pager_with_the_installed_versions_entries(
    changelog_file: Path,
) -> None:
    shown = _dispatch(AppState.create())

    assert len(shown) == 1
    assert isinstance(shown[0], ReleaseNotesOverlay)
    text = _text(shown[0])
    assert "Fix number three." in text
    assert "Work that is in this build" in text
    assert "Feature number one." not in text


def test_the_next_view_lists_everything_since_the_last_one_seen(changelog_file: Path) -> None:
    save_user_state(UserState(version="0.3.0", release_notes_seen="0.1.0"))

    first = _text(_dispatch()[0])
    assert "Feature number two." in first
    assert load_user_state().release_notes_seen == "0.3.0"

    second = _text(_dispatch()[0])
    assert "Feature number two." not in second
    assert "Fix number three." in second
