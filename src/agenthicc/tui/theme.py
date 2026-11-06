"""Design tokens, themes and glyphs for every TUI surface (TUX-01).

Every colour the TUI draws comes from a semantic **token** of the active
:class:`Theme`; no other module in ``agenthicc.tui`` names a colour.  Tokens
resolve to Rich style strings, so they work in markup (``f"[{p.accent}]x"``),
in ``Text(style=...)`` and, through :func:`style`, as :class:`rich.style.Style`
objects for segment-level rendering.

Glyphs live here too: the ``unicode`` set uses only characters present in the
common monospace fonts (DejaVu Sans Mono, Noto Sans Mono, Menlo, Consolas,
Cascadia Mono); the ``ascii`` set uses nothing above U+007F.

The active theme is process-wide.  :func:`use_theme` swaps it at runtime and
notifies listeners (the live region redraws), so ``/theme`` needs no restart.
"""

from __future__ import annotations

import os
import threading
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path

from rich.color import Color, ColorParseError
from rich.style import Style

__all__ = [
    "BUILTIN_THEMES",
    "Glyphs",
    "Palette",
    "Theme",
    "ThemeError",
    "THEME_BACKGROUNDS",
    "TOKENS",
    "ASCII_GLYPHS",
    "UNICODE_GLYPHS",
    "available_themes",
    "background_is_light",
    "contrast_ratio",
    "current_theme",
    "glyphs",
    "load_theme",
    "on_theme_change",
    "palette",
    "resolve_theme",
    "style",
    "use_theme",
]


class ThemeError(ValueError):
    """A custom theme file is invalid; the message names file, line and token."""


@dataclass(frozen=True)
class Palette:
    """Semantic colour tokens, each a Rich style string (``"#7aa2f7"``, ``"none"``)."""

    text: str
    muted: str
    subtle: str
    accent: str
    accent_bg: str
    user_bg: str
    success: str
    warning: str
    danger: str
    info: str
    agent: str
    diff_add: str
    diff_del: str
    diff_add_bg: str
    diff_del_bg: str
    diff_add_word: str
    diff_del_word: str
    mode_safe: str
    mode_plan: str
    mode_yolo: str

    def get(self, token: str) -> str:
        """The style string of *token* (``KeyError`` for an unknown token)."""
        values: dict[str, str] = vars(self)
        if token not in values:
            raise KeyError(f"unknown theme token {token!r}")
        return values[token]

    def on(self, token: str) -> str:
        """The token as a background style (``"on #1c1f24"``), or ``"none"``."""
        value = self.get(token)
        return "none" if value in ("", "none", "default") else f"on {value}"


#: Every token name, in declaration order.
TOKENS: tuple[str, ...] = tuple(field.name for field in fields(Palette))


@dataclass(frozen=True)
class Glyphs:
    """Every non-ASCII character the TUI chrome draws."""

    prompt: str
    bullet: str
    elbow: str
    pipe: str
    ok: str
    fail: str
    warn: str
    info: str
    diamond: str
    spinner: tuple[str, ...]
    rule: str
    ellipsis: str
    up: str
    down: str
    left: str
    right: str
    cursor: str
    bar: str
    box: tuple[str, str, str, str, str, str]
    """Corners and edges: top-left, top-right, bottom-left, bottom-right, horizontal, vertical."""
    meter_full: str
    meter_empty: str
    sep: str
    approx: str
    """Marks an approximate value, such as partially reported token usage."""


UNICODE_GLYPHS = Glyphs(
    prompt="❯",
    bullet="●",
    elbow="└",
    pipe="│",
    ok="✓",
    fail="✗",
    warn="!",
    info="·",
    diamond="◆",
    # The flower agenthicc has always turned while it works (kept by request).
    spinner=("✿", "❀", "❁", "❃", "✾", "❋", "✽", "❊"),
    rule="─",
    ellipsis="…",
    up="↑",
    down="↓",
    left="←",
    right="→",
    cursor="▌",
    bar="▌",
    box=("╭", "╮", "╰", "╯", "─", "│"),
    meter_full="▰",
    meter_empty="▱",
    sep="·",
    approx="≈",
)

ASCII_GLYPHS = Glyphs(
    prompt=">",
    bullet="*",
    elbow="`",
    pipe="|",
    ok="+",
    fail="x",
    warn="!",
    info="-",
    diamond="*",
    spinner=("|", "/", "-", "\\"),
    rule="-",
    ellipsis="...",
    up="^",
    down="v",
    left="<",
    right=">",
    cursor="_",
    bar="|",
    box=("+", "+", "+", "+", "-", "|"),
    meter_full="#",
    meter_empty=".",
    sep="-",
    approx="~",
)


@dataclass(frozen=True)
class Theme:
    """A named palette, its syntax-highlighting style and its glyph set."""

    name: str
    palette: Palette
    syntax_theme: str
    glyphs: Glyphs = UNICODE_GLYPHS
    light: bool = False

    def with_glyphs(self, glyph_set: Glyphs) -> Theme:
        return replace(self, glyphs=glyph_set)


_DARK = Palette(
    text="none",
    muted="#8a8f98",
    subtle="#636a75",
    accent="#7aa2f7",
    accent_bg="#1f2a44",
    user_bg="#1c1f24",
    success="#9ece6a",
    warning="#e0af68",
    danger="#f7768e",
    info="#7dcfff",
    agent="#bb9af7",
    diff_add="#9ece6a",
    diff_del="#f7768e",
    diff_add_bg="#153a15",
    diff_del_bg="#3a1515",
    diff_add_word="#206a20",
    diff_del_word="#6a2020",
    mode_safe="#9ece6a",
    mode_plan="#7dcfff",
    mode_yolo="#f7768e",
)

_LIGHT = Palette(
    text="none",
    muted="#5c6370",
    subtle="#8c939d",
    accent="#2f5bd3",
    accent_bg="#e3ebff",
    user_bg="#f1f3f5",
    success="#2e7d32",
    warning="#a15c00",
    danger="#c62828",
    info="#00639b",
    agent="#7b3fbf",
    diff_add="#2e7d32",
    diff_del="#c62828",
    diff_add_bg="#e6f4e7",
    diff_del_bg="#fbe9e9",
    diff_add_word="#b7e3ba",
    diff_del_word="#f5c2c2",
    mode_safe="#2e7d32",
    mode_plan="#00639b",
    mode_yolo="#c62828",
)

_HIGH_CONTRAST = Palette(
    text="none",
    muted="#d0d0d0",
    subtle="#9a9a9a",
    accent="#7fbfff",
    accent_bg="#00305f",
    user_bg="#202020",
    success="#7dff7d",
    warning="#ffd75f",
    danger="#ff8080",
    info="#80e0ff",
    agent="#e0a0ff",
    diff_add="#7dff7d",
    diff_del="#ff8080",
    diff_add_bg="#003000",
    diff_del_bg="#400000",
    diff_add_word="#006000",
    diff_del_word="#800000",
    mode_safe="#7dff7d",
    mode_plan="#80e0ff",
    mode_yolo="#ff8080",
)

#: Only the 16 terminal palette colours: inherits the user's terminal scheme.
_ANSI = Palette(
    text="none",
    muted="bright_black",
    subtle="bright_black",
    accent="blue",
    accent_bg="none",
    user_bg="none",
    success="green",
    warning="yellow",
    danger="red",
    info="cyan",
    agent="magenta",
    diff_add="green",
    diff_del="red",
    diff_add_bg="none",
    diff_del_bg="none",
    diff_add_word="none",
    diff_del_word="none",
    mode_safe="green",
    mode_plan="cyan",
    mode_yolo="red",
)

#: ``NO_COLOR``: every token removed; bold, dim and reverse still work.
_NO_COLOR = Palette(**dict.fromkeys(TOKENS, "none"))

BUILTIN_THEMES: Mapping[str, Theme] = {
    "dark": Theme("dark", _DARK, "monokai"),
    "light": Theme("light", _LIGHT, "friendly", light=True),
    "high-contrast": Theme("high-contrast", _HIGH_CONTRAST, "native"),
    "ansi": Theme("ansi", _ANSI, "ansi_dark"),
    "no-color": Theme("no-color", _NO_COLOR, "bw"),
}

#: Reference backgrounds the contrast tests measure against.
THEME_BACKGROUNDS: Mapping[str, tuple[int, int, int]] = {
    "dark": (24, 24, 27),
    "light": (255, 255, 255),
    "high-contrast": (0, 0, 0),
}


# ── active theme ──────────────────────────────────────────────────────────────

_lock = threading.Lock()
_current: Theme = BUILTIN_THEMES["dark"]
_listeners: list[Callable[[Theme], None]] = []


def current_theme() -> Theme:
    """The theme every renderer uses right now."""
    return _current


def palette() -> Palette:
    """The active theme's colour tokens."""
    return _current.palette


def glyphs() -> Glyphs:
    """The active theme's glyph set."""
    return _current.glyphs


def style(token: str, *, bold: bool = False, background: str | None = None) -> Style:
    """The *token* of the active palette as a :class:`Style` (unknown → error)."""
    value = _current.palette.get(token)
    base = Style.parse(value) if value not in ("", "default") else Style.null()
    if bold:
        base = base + Style(bold=True)
    if background is not None:
        base = base + Style.parse(_current.palette.on(background))
    return base


def use_theme(theme: Theme) -> None:
    """Make *theme* active and notify every listener (no restart needed)."""
    global _current
    with _lock:
        _current = theme
        listeners = list(_listeners)
    for listener in listeners:
        listener(theme)


def on_theme_change(listener: Callable[[Theme], None]) -> Callable[[], None]:
    """Call *listener* after each :func:`use_theme`; return an unsubscribe."""
    with _lock:
        _listeners.append(listener)

    def unsubscribe() -> None:
        with _lock:
            if listener in _listeners:
                _listeners.remove(listener)

    return unsubscribe


# ── custom themes ─────────────────────────────────────────────────────────────


def _theme_dirs(cwd: Path | None) -> tuple[Path, ...]:
    """Project directory first: a project theme overrides a user theme."""
    home = Path(os.environ.get("AGENTHICC_HOME", Path.home() / ".agenthicc"))
    project = (cwd or Path.cwd()) / ".agenthicc" / "themes"
    return (project, home / "themes")


def available_themes(cwd: Path | None = None) -> tuple[str, ...]:
    """Built-in theme names followed by custom ones (project overrides user)."""
    names = [name for name in BUILTIN_THEMES if name != "no-color"]
    for directory in _theme_dirs(cwd):
        if directory.is_dir():
            for path in sorted(directory.glob("*.toml")):
                if path.stem not in names:
                    names.append(path.stem)
    return tuple(names)


def _line_of(text: str, key: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(key):
            return number
    return 0


def _parse_theme_file(path: Path) -> Theme:
    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ThemeError(f"{path}: {exc}") from exc
    base_name = data.get("extends", "dark")
    if not isinstance(base_name, str) or base_name not in BUILTIN_THEMES:
        raise ThemeError(f"{path}:{_line_of(text, 'extends')}: unknown base theme {base_name!r}")
    base = BUILTIN_THEMES[base_name]
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        raise ThemeError(f"{path}: [tokens] must be a table")
    overrides: dict[str, str] = {}
    for token, value in tokens.items():
        line = _line_of(text, token)
        if token not in TOKENS:
            raise ThemeError(f"{path}:{line}: unknown token {token!r}")
        if not isinstance(value, str):
            raise ThemeError(f"{path}:{line}: token {token!r} must be a colour string")
        if value not in ("none", "default"):
            try:
                Color.parse(value)
            except ColorParseError as exc:
                raise ThemeError(f"{path}:{line}: invalid colour {value!r} for {token!r}") from exc
        overrides[token] = value
    syntax = data.get("syntax_theme", base.syntax_theme)
    if not isinstance(syntax, str):
        raise ThemeError(f"{path}:{_line_of(text, 'syntax_theme')}: syntax_theme must be a string")
    return Theme(
        path.stem,
        replace(base.palette, **overrides),
        syntax,
        light=bool(data.get("light", base.light)),
    )


def load_theme(name: str, cwd: Path | None = None) -> Theme:
    """A built-in or custom theme by *name*; raises :class:`ThemeError`."""
    if name in BUILTIN_THEMES:
        return BUILTIN_THEMES[name]
    for directory in _theme_dirs(cwd):
        path = directory / f"{name}.toml"
        if path.is_file():
            return _parse_theme_file(path)
    raise ThemeError(f"unknown theme {name!r}")


# ── automatic selection ───────────────────────────────────────────────────────


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two sRGB colours."""
    a, b = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def background_is_light(rgb: tuple[int, int, int] | None, env: Mapping[str, str]) -> bool | None:
    """Whether the terminal background is light: OSC 11 answer, then ``COLORFGBG``."""
    if rgb is not None:
        return _relative_luminance(rgb) > 0.4
    colorfgbg = env.get("COLORFGBG", "")
    if colorfgbg:
        last = colorfgbg.split(";")[-1]
        if last.isdigit():
            # xterm convention: 0–6 and 8 are dark backgrounds, 7 and 9–15 light.
            return int(last) in {7, 9, 10, 11, 12, 13, 14, 15}
    return None


def _use_ascii(glyph_setting: str, encoding: str, screen_reader: bool) -> bool:
    """ASCII glyphs for a screen reader, on request, or when output is not UTF.

    *encoding* is the encoding text is written in (Rich's ``console.encoding``),
    the same test Rich uses for its own box characters.  ``TERM`` says nothing
    reliable about the font: many Unicode-capable terminals report ``linux``.
    """
    if glyph_setting == "ascii" or screen_reader:
        return True
    if glyph_setting == "unicode":
        return False
    return not encoding.lower().replace("-", "").startswith("utf")


def resolve_theme(
    name: str,
    glyph_setting: str,
    *,
    env: Mapping[str, str],
    screen_reader: bool = False,
    background: tuple[int, int, int] | None = None,
    cwd: Path | None = None,
    encoding: str = "utf-8",
) -> tuple[Theme, tuple[str, ...]]:
    """The theme to start with, plus notices for anything that fell back.

    ``NO_COLOR`` (any value) wins over every setting.  ``auto`` uses the
    terminal background (*background*, from OSC 11) or ``COLORFGBG``, else
    ``dark``.  A broken custom theme falls back to ``auto`` with a notice.
    *encoding* is the output encoding; ``glyphs = "auto"`` uses ASCII when it
    is not a UTF encoding.
    """
    notices: list[str] = []
    theme: Theme | None = None
    if "NO_COLOR" in env:
        theme = BUILTIN_THEMES["no-color"]
    else:
        if name != "auto":
            try:
                theme = load_theme(name, cwd)
            except ThemeError as exc:
                notices.append(f"{exc}; using the automatic theme")
            except OSError as exc:
                notices.append(
                    f"theme {name!r} could not be read ({exc}); using the automatic theme"
                )
        if theme is None:
            light = background_is_light(background, env)
            theme = BUILTIN_THEMES["light" if light else "dark"]
    glyph_set = (
        ASCII_GLYPHS if _use_ascii(glyph_setting, encoding, screen_reader) else UNICODE_GLYPHS
    )
    return theme.with_glyphs(glyph_set), tuple(notices)
