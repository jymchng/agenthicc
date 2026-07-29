"""Unit tests for bounded unified-diff previews."""

from __future__ import annotations

from rich.console import Console

import pytest

from agenthicc.tui.diff_renderer import DIFF_PREVIEW_LINES, render_file_diff

pytestmark = pytest.mark.unit


def _render(old_lines: list[str], new_lines: list[str]) -> str:
    console = Console(record=True, width=120, color_system=None)
    console.print(render_file_diff("README.md", old_lines, new_lines, language="text"))
    return console.export_text()


def test_long_addition_shows_edges_and_one_omission_marker() -> None:
    output = _render(
        ["title"],
        ["title", *[f"added {index}" for index in range(1, 11)]],
    )

    for line in ("added 1", "added 2", "added 3", "added 8", "added 9", "added 10"):
        assert line in output
    for line in ("added 4", "added 5", "added 6", "added 7"):
        assert line not in output
    assert output.count("...") == 1
    assert "4 more diff lines" in output


def test_change_at_preview_limit_is_not_collapsed() -> None:
    lines = [f"changed {index}" for index in range(DIFF_PREVIEW_LINES)]

    output = _render([], lines)

    for line in lines:
        assert line in output
    assert "more diff lines" not in output
    assert "..." not in output


def test_long_replacement_is_one_change_block() -> None:
    output = _render(
        [f"old-{index:02d}" for index in range(1, 13)],
        [f"new-{index:02d}" for index in range(1, 13)],
    )

    for line in ("old-01", "old-02", "old-03", "new-10", "new-11", "new-12"):
        assert line in output
    for line in ("old-04", "old-12", "new-01", "new-09"):
        assert line not in output
    assert output.count("...") == 1
    assert "18 more diff lines" in output


def test_separate_change_blocks_get_separate_omission_markers() -> None:
    output = _render(
        ["same", *[f"old {index}" for index in range(1, 4)], "middle", "tail"],
        ["same", *[f"new {index}" for index in range(1, 4)], "middle", "tail"],
    )

    assert output.count("...") == 0

    output = _render(
        [
            "same",
            *[f"old {index}" for index in range(1, 5)],
            "middle",
            *[f"old tail {index}" for index in range(1, 5)],
            "tail",
        ],
        [
            "same",
            *[f"new {index}" for index in range(1, 11)],
            "middle",
            *[f"new tail {index}" for index in range(1, 11)],
            "tail",
        ],
    )

    assert output.count("...") == 2
