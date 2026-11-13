"""An embedded composer draws one prompt, with or without a picker open.

The trigger picker (and Ctrl+R history search) draws its own prompt line, so
the main workspace shows the overlay *instead of* the composer. The agents
hub and the answer composer used to draw the overlay and then the composer
again, so ``/`` and ``@`` showed the input panel twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console, RenderableType

from agenthicc.commands import build_builtin_registry
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.input.embedded import ComposerContext, EmbeddedComposer
from agenthicc.tui.theme import glyphs

pytestmark = pytest.mark.unit


async def _submit(_text: str) -> bool:
    return True


def _plain(renderables: list[RenderableType]) -> list[str]:
    console = Console(record=True, width=80, color_system=None, highlight=False)
    for renderable in renderables:
        console.print(renderable)
    return console.export_text().splitlines()


def _prompt_rows(composer: EmbeddedComposer) -> list[str]:
    chevron = glyphs().prompt
    return [row for row in _plain(composer.render()) if row.startswith(chevron)]


def _composer(tmp_path: Path) -> EmbeddedComposer:
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    context = ComposerContext(cwd=tmp_path, commands=build_builtin_registry())
    return EmbeddedComposer(context, on_submit=_submit)


async def _type(composer: EmbeddedComposer, keys: list[tuple[Key, str]]) -> None:
    for key, ch in keys:
        composer.feed(key, ch)
        await composer.settle()


async def test_one_prompt_while_nothing_is_open(tmp_path: Path) -> None:
    composer = _composer(tmp_path)
    await _type(composer, [(Key.CHAR, "h"), (Key.CHAR, "i")])

    assert not composer.picker_open
    assert len(_prompt_rows(composer)) == 1


async def test_one_prompt_while_the_mention_picker_is_open(tmp_path: Path) -> None:
    composer = _composer(tmp_path)
    await _type(composer, [(Key.CHAR, "s"), (Key.CHAR, " "), (Key.AT, "")])

    assert composer.picker_open
    rows = _prompt_rows(composer)
    assert len(rows) == 1, rows
    assert "@" in rows[0]


async def test_one_prompt_while_the_command_picker_is_open(tmp_path: Path) -> None:
    composer = _composer(tmp_path)
    await _type(composer, [(Key.CHAR, "/"), (Key.CHAR, "h")])

    assert composer.picker_open
    rows = _prompt_rows(composer)
    assert len(rows) == 1, rows
    assert "/h" in rows[0]


async def test_a_long_prompt_is_not_repeated_under_the_picker(tmp_path: Path) -> None:
    composer = _composer(tmp_path)
    composer.set_text("anyway, write a PRD to explain why this error occurs, in prds/ ")
    await _type(composer, [(Key.AT, "")])

    assert composer.picker_open
    rendered = "\n".join(_plain(composer.render()))
    assert rendered.count("write a PRD") == 1


async def test_the_prompt_returns_when_the_picker_closes(tmp_path: Path) -> None:
    composer = _composer(tmp_path)
    await _type(composer, [(Key.CHAR, "/"), (Key.ESC, "")])

    assert not composer.picker_open
    assert len(_prompt_rows(composer)) == 1
