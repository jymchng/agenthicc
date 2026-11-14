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


def test_a_sixteen_colour_render_does_not_poison_a_truecolor_one() -> None:
    """Rich caches ANSI codes per Style object; the conftest clears the shared caches."""
    from io import StringIO

    from rich.console import Console
    from rich.text import Text

    from tests.conftest import _clear_rich_style_caches

    def render(system: str) -> str:
        console = Console(file=StringIO(), record=True, width=20, color_system=system)  # type: ignore[arg-type]
        console.print(Text("x", style="bold #7aa2f7"))
        return console.export_text(styles=True)

    assert "38;2;" not in render("standard")
    # Without the clear, the shared Style would still hold the 16-colour codes.
    _clear_rich_style_caches()
    assert "38;2;122;162;247" in render("truecolor")


def test_the_next_test_still_renders_truecolor_after_a_sixteen_colour_one() -> None:
    from io import StringIO

    from rich.console import Console
    from rich.text import Text

    console = Console(file=StringIO(), record=True, width=20, color_system="truecolor")
    console.print(Text("x", style="bold #7aa2f7"))
    assert "38;2;122;162;247" in console.export_text(styles=True)
