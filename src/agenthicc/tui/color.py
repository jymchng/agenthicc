from __future__ import annotations

import re
from dataclasses import dataclass

_ANSI_ESCAPE = re.compile(
    r'\x1b\[[0-9;]*[mABCDEFGHJKSTfhilmnprsu]'
    r'|\x1b\][^\x07]*\x07'
    r'|\x1b[()][A-Z0-9]'
)


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return _ANSI_ESCAPE.sub('', text)


def clip_ansi_line(text: str, cols: int) -> str:
    """Clip text to cols display columns, preserving ANSI sequences."""
    from .terminal import _clip_to_cols
    return _clip_to_cols(text, cols)


@dataclass(frozen=True)
class ANSIColor:
    """Named ANSI color with depth-aware rendering."""

    name: str          # rich color name e.g. "bright_blue"
    ansi_256: int      # 256-color code
    ansi_true: str     # truecolor hex e.g. "#0087ff"

    def render(self, text: str, *, depth: int = 8) -> str:
        """Wrap text in appropriate ANSI escape for color depth."""
        if depth == 0:
            return text
        from rich.style import Style  # noqa: F401
        from rich.text import Text
        from rich.console import Console
        import io
        buf = io.StringIO()
        c = Console(
            file=buf,
            highlight=False,
            markup=False,
            force_terminal=True,
            color_system="256" if depth <= 256 else "truecolor",
        )
        t = Text(text, style=self.name)
        c.print(t, end="")
        return buf.getvalue()


@dataclass
class ColorPalette:
    """Color palette for the TUI."""

    primary: str = "bright_blue"
    success: str = "bright_green"
    warning: str = "bright_yellow"
    error: str = "bright_red"
    muted: str = "bright_black"
    accent: str = "bright_cyan"


def color_for_depth(color_name: str, depth: int) -> str:
    """Return Rich color string appropriate for the given depth."""
    if depth == 0:
        return ""
    return color_name
