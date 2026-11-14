"""One test's theme never reaches the next (the conftest restores it)."""

from __future__ import annotations

import pytest

from agenthicc.tui.theme import BUILTIN_THEMES, current_theme, use_theme

pytestmark = pytest.mark.unit

_FIRST: list[object] = []


def test_a_test_may_install_a_theme() -> None:
    _FIRST.append(current_theme())
    use_theme(BUILTIN_THEMES["no-color"])
    assert current_theme() is BUILTIN_THEMES["no-color"]


def test_the_next_test_starts_with_the_theme_the_first_one_started_with() -> None:
    assert _FIRST, "runs after the test above"
    assert current_theme() is _FIRST[0]
