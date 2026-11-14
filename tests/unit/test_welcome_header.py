"""TUX-03 §2.1 and §2.2: a compact header that says only what is true, and no placeholder text."""

from __future__ import annotations

import io
import re
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.tui.theme import ASCII_GLYPHS, BUILTIN_THEMES, use_theme
from agenthicc.tui.welcome import (
    MASCOT,
    WelcomeInfo,
    home_relative,
    middle_truncate,
    print_welcome,
    render_welcome,
)

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "agenthicc"

_INFO = WelcomeInfo(
    version="0.42.0",
    model="opus-4.8",
    provider="anthropic",
    mode="Safe",
    directory=Path("/home/me/python_projects/agenthicc"),
    home=Path("/home/me"),
    branch="main",
)


def _rows(info: WelcomeInfo, width: int) -> list[str]:
    console = Console(width=width, file=io.StringIO(), force_terminal=False, color_system=None)
    console.print(render_welcome(info, width))
    return console.file.getvalue().splitlines()  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _dark_theme() -> None:
    use_theme(BUILTIN_THEMES["dark"])


# ── AC 1: height ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("width", [40, 50, 59, 60, 80, 100, 120, 200])
def test_the_header_is_at_most_three_rows_after_the_first_run(width: int) -> None:
    rows = _rows(replace(_INFO, whats_new=True), width)

    assert len(rows) <= 3
    assert all(len(row) <= width for row in rows)


@pytest.mark.parametrize("width", [40, 59, 60, 80, 120])
def test_the_first_run_header_is_at_most_four_rows(width: int) -> None:
    rows = _rows(replace(_INFO, mascot=True, whats_new=True), width)

    assert len(rows) <= 4
    assert all(len(row) <= width for row in rows)


def test_a_notice_row_after_the_header_stays_within_the_budget() -> None:
    from agenthicc.tui.workspace.notice import Notice

    console = Console(width=120, file=io.StringIO(), force_terminal=False, color_system=None)
    console.print(render_welcome(_INFO, 120))
    console.print(Notice("warning", "No API key for anthropic. Set ANTHROPIC_API_KEY."))
    rows = [row for row in console.file.getvalue().splitlines() if row.strip()]  # type: ignore[attr-defined]

    assert len(rows) == 4  # three header rows and one notice row


# ── the content ───────────────────────────────────────────────────────────────


def test_the_three_lines() -> None:
    assert _rows(_INFO, 120) == [
        " ◆ agenthicc 0.42.0",
        "   opus-4.8 · anthropic · Safe mode",
        "   ~/python_projects/agenthicc  (main)",
    ]


def test_the_directory_is_cut_in_the_middle_and_keeps_its_last_segment() -> None:
    assert middle_truncate("~/python_projects/agenthicc", 15) == "~/py…/agenthicc"
    assert middle_truncate("~/short", 15) == "~/short"
    narrow = _rows(replace(_INFO, directory=Path("/home/me/a/very/long/path/to/agenthicc")), 40)
    assert narrow[2].endswith("/agenthicc  (main)")
    assert "…" in narrow[2]


def test_the_home_directory_is_written_as_a_tilde() -> None:
    home = Path("/home/me")
    assert home_relative(home, home) == "~"
    assert home_relative(home / "work" / "app", home) == "~/work/app"
    assert home_relative(Path("/srv/app"), home) == "/srv/app"


def test_there_is_no_branch_outside_a_repository() -> None:
    rows = _rows(replace(_INFO, branch=""), 120)

    assert rows[2] == "   ~/python_projects/agenthicc"


def test_the_whats_new_hint_is_right_aligned_and_only_after_an_upgrade() -> None:
    plain = _rows(_INFO, 100)
    upgraded = _rows(replace(_INFO, whats_new=True), 100)

    assert "what's new" not in plain[0]
    assert upgraded[0].endswith("what's new: /release-notes")
    assert len(upgraded[0]) == 100 - 1  # right margin of one cell


def test_the_hint_is_dropped_rather_than_crowding_the_name() -> None:
    assert "what's new" not in _rows(replace(_INFO, whats_new=True), 40)[0]


def test_a_narrow_terminal_merges_lines_two_and_three() -> None:
    rows = _rows(_INFO, 40)

    assert rows[0] == " ◆ agenthicc 0.42.0"
    merged = " ".join(row.strip() for row in rows[1:])
    assert "opus-4.8 · anthropic · Safe mode" in merged
    assert "(main)" in merged
    assert not any(row.rstrip().endswith("·") for row in rows)


def test_the_mascot_and_tagline_only_when_asked() -> None:
    assert not any("state-driven" in row for row in _rows(_INFO, 120))
    first_run = _rows(replace(_INFO, mascot=True), 120)

    assert any("state-driven agent operating system" in row for row in first_run)
    for art, row in zip(MASCOT, first_run, strict=False):
        assert art.strip() in row


def test_banner_off_is_the_callers_to_honour_so_print_welcome_always_prints() -> None:
    console = Console(width=80, file=io.StringIO(), force_terminal=False, color_system=None)

    print_welcome(console, _INFO)

    assert "agenthicc 0.42.0" in console.file.getvalue()  # type: ignore[attr-defined]


# ── glyphs and tokens ─────────────────────────────────────────────────────────


def test_the_header_is_pure_ascii_under_the_ascii_glyph_set() -> None:
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    for info in (_INFO, replace(_INFO, mascot=True, whats_new=True)):
        for width in (40, 120):
            text = "\n".join(_rows(info, width))
            assert text.isascii(), text


def test_colours_come_from_the_active_theme() -> None:
    def styles(theme_name: str) -> set[str]:
        use_theme(BUILTIN_THEMES[theme_name])
        console = Console(
            width=100, file=io.StringIO(), force_terminal=True, color_system="truecolor"
        )
        console.print(render_welcome(replace(_INFO, whats_new=True), 100))
        return set(re.findall(r"\x1b\[[0-9;]*m", console.file.getvalue()))  # type: ignore[attr-defined]

    assert styles("dark") != styles("light")


# ── AC 2: no placeholders, no network ─────────────────────────────────────────

_FORBIDDEN = ("No list", "Welcome back!", "a AGENTS.md")
_DELETED_NAMES = (
    "fetch_changelog",
    "load_cached_changelog",
    "CHANGELOG_URL",
    "changelog.json",
    "AGENTHICC_CHANGELOG_CACHE",
    "_normalize_changelog",
)


def _sources() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def test_no_placeholder_text_appears_anywhere_in_src() -> None:
    found = [
        f"{path.relative_to(_SRC)}: {phrase}"
        for path in _sources()
        for phrase in _FORBIDDEN
        if phrase in path.read_text(encoding="utf-8")
    ]

    assert found == []


def test_the_remote_changelog_is_gone_from_src_and_scripts() -> None:
    scripts = _SRC.parents[1] / "scripts"
    paths = [*_sources(), *sorted(scripts.glob("*.py"))]
    found = [
        f"{path.name}: {name}"
        for path in paths
        for name in _DELETED_NAMES
        if name in path.read_text(encoding="utf-8")
    ]

    assert found == []
