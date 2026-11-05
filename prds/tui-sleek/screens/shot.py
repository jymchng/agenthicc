"""Render a tmux pane, or a saved ANSI capture, to a PNG with its colours.

Usage::

    python shot.py SESSION OUT.png [-S start -E end]   # a live tmux pane
    python shot.py CAPTURE.ansi OUT.png [COLUMNS]      # a saved capture

Needs Pillow, Rich, DejaVu Sans Mono and Noto Sans Mono. A character missing
from both fonts renders as tofu, which is how TUX-00 finding F4 was confirmed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rich.color import Color, ColorType
from rich.style import Style
from rich.terminal_theme import MONOKAI
from rich.text import Text

_FONTS = Path("/usr/share/fonts/truetype")
_BG = (24, 24, 27)
_FG = (220, 220, 220)
_CELL_H = 20


def _capture(source: str, extra: list[str]) -> tuple[str, int]:
    if source.endswith(".ansi"):
        cols = int(extra[0]) if extra else 120
        return Path(source).read_text(encoding="utf-8"), cols
    raw = subprocess.run(
        ["tmux", "capture-pane", "-p", "-e", "-t", source, *extra],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    width = subprocess.run(
        ["tmux", "display", "-p", "-t", source, "#{pane_width}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return raw, int(width)


def _rgb(color: Color | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if color is None or color.type == ColorType.DEFAULT:
        return default
    truecolor = color.get_truecolor(MONOKAI)
    return (truecolor.red, truecolor.green, truecolor.blue)


def render(raw: str, cols: int, out: str) -> tuple[int, int]:
    """Draw *raw* ANSI text, *cols* cells wide, into the PNG at *out*."""
    font = ImageFont.truetype(str(_FONTS / "dejavu/DejaVuSansMono.ttf"), 15)
    bold = ImageFont.truetype(str(_FONTS / "dejavu/DejaVuSansMono-Bold.ttf"), 15)
    fallback = ImageFont.truetype(str(_FONTS / "noto/NotoSansMono-Regular.ttf"), 15)
    cell_w = int(font.getlength("M"))
    rows = raw.split("\n")
    image = Image.new("RGB", (cell_w * cols + 20, _CELL_H * len(rows) + 20), _BG)
    draw = ImageDraw.Draw(image)
    for y, line in enumerate(rows):
        text = Text.from_ansi(line)
        styles = [Style()] * len(text.plain)
        for span in text.spans:
            span_style = span.style if isinstance(span.style, Style) else Style.parse(span.style)
            for index in range(span.start, min(span.end, len(styles))):
                styles[index] = styles[index] + span_style
        for x, char in enumerate(text.plain):
            style = styles[x]
            fg = _rgb(style.color, _FG)
            bg = _rgb(style.bgcolor, _BG)
            if style.reverse:
                fg, bg = bg, fg
            if style.dim:
                fg = (
                    int(fg[0] * 0.6 + _BG[0] * 0.4),
                    int(fg[1] * 0.6 + _BG[1] * 0.4),
                    int(fg[2] * 0.6 + _BG[2] * 0.4),
                )
            px, py = 10 + x * cell_w, 10 + y * _CELL_H
            if bg != _BG:
                draw.rectangle([px, py, px + cell_w, py + _CELL_H], fill=bg)
            face = bold if style.bold else font
            if char.strip() and face.getmask(char).getbbox() is None:
                face = fallback
            draw.text((px, py + 2), char, font=face, fill=fg)
    image.save(out)
    return image.size


def main(argv: list[str]) -> None:
    source, out, *extra = argv
    raw, cols = _capture(source, extra)
    print(out, render(raw, cols, out))


if __name__ == "__main__":
    main(sys.argv[1:])
