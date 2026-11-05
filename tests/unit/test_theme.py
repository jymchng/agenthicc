"""TUX-01: design tokens, themes and glyphs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from rich.color import Color
from rich.console import Console

from agenthicc.tui import theme as theme_module
from agenthicc.tui.theme import (
    ASCII_GLYPHS,
    BUILTIN_THEMES,
    THEME_BACKGROUNDS,
    TOKENS,
    UNICODE_GLYPHS,
    Glyphs,
    ThemeError,
    available_themes,
    background_is_light,
    contrast_ratio,
    current_theme,
    load_theme,
    on_theme_change,
    palette,
    resolve_theme,
    style,
    use_theme,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_theme() -> object:
    before = current_theme()
    yield
    use_theme(before)


def _rgb(value: str) -> tuple[int, int, int]:
    color = Color.parse(value).get_truecolor()
    return (color.red, color.green, color.blue)


@pytest.mark.parametrize("name", sorted(THEME_BACKGROUNDS))
def test_every_theme_meets_wcag_contrast(name: str) -> None:
    theme = BUILTIN_THEMES[name]
    background = THEME_BACKGROUNDS[name]
    body = 7.0 if name == "high-contrast" else 4.5
    for token in ("muted", "accent", "success", "warning", "danger", "info", "agent"):
        ratio = contrast_ratio(_rgb(getattr(theme.palette, token)), background)
        assert ratio >= body, f"{name}.{token} = {ratio:.2f}"
    assert contrast_ratio(_rgb(theme.palette.subtle), background) >= 3.0


def test_every_builtin_theme_defines_every_token_as_a_parseable_style() -> None:
    from rich.style import Style

    for theme in BUILTIN_THEMES.values():
        for token in TOKENS:
            value = getattr(theme.palette, token)
            Style.parse(value)
            Style.parse(theme.palette.on(token))


def test_glyph_sets_are_complete_and_ascii_is_ascii() -> None:
    from dataclasses import fields

    for field in fields(Glyphs):
        assert getattr(ASCII_GLYPHS, field.name)
        assert getattr(UNICODE_GLYPHS, field.name)

    def chars(glyph_set: Glyphs) -> str:
        out = ""
        for field in fields(Glyphs):
            value = getattr(glyph_set, field.name)
            out += "".join(value) if isinstance(value, tuple) else value
        return out

    assert all(ord(ch) < 0x80 for ch in chars(ASCII_GLYPHS))


_DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")


@pytest.mark.skipif(not _DEJAVU.exists(), reason="DejaVu Sans Mono is not installed")
def test_every_unicode_glyph_is_in_dejavu_sans_mono() -> None:
    pil = pytest.importorskip("PIL.ImageFont")
    from dataclasses import fields

    font = pil.truetype(str(_DEJAVU), 15)
    missing: list[str] = []
    for field in fields(Glyphs):
        value = getattr(UNICODE_GLYPHS, field.name)
        for ch in value if isinstance(value, tuple) else (value,):
            for single in ch:
                if single.strip() and font.getmask(single).getbbox() is None:
                    missing.append(f"{field.name}: U+{ord(single):04X}")
    assert missing == []


def test_style_resolves_tokens_and_rejects_unknown_ones() -> None:
    use_theme(BUILTIN_THEMES["dark"])
    accent = style("accent", bold=True)
    assert accent.bold and accent.color is not None
    assert style("text").color is None
    with pytest.raises(KeyError):
        style("acent")


def test_use_theme_notifies_listeners_and_unsubscribes() -> None:
    seen: list[str] = []
    unsubscribe = on_theme_change(lambda theme: seen.append(theme.name))
    use_theme(BUILTIN_THEMES["light"])
    unsubscribe()
    use_theme(BUILTIN_THEMES["dark"])
    assert seen == ["light"]
    assert palette() is BUILTIN_THEMES["dark"].palette


def test_no_color_wins_over_every_setting() -> None:
    theme, notices = resolve_theme("light", "auto", env={"NO_COLOR": "1"})
    assert theme.name == "no-color" and notices == ()
    assert all(getattr(theme.palette, token) == "none" for token in TOKENS)


@pytest.mark.parametrize(
    ("background", "env", "expected"),
    [
        ((255, 255, 255), {}, "light"),
        ((0, 0, 0), {}, "dark"),
        (None, {"COLORFGBG": "0;15"}, "light"),
        (None, {"COLORFGBG": "15;0"}, "dark"),
        (None, {}, "dark"),
    ],
)
def test_auto_follows_the_terminal_background(
    background: tuple[int, int, int] | None, env: dict[str, str], expected: str
) -> None:
    theme, _ = resolve_theme("auto", "auto", env=env, background=background)
    assert theme.name == expected


def test_background_detection_ignores_garbage() -> None:
    assert background_is_light(None, {"COLORFGBG": "default"}) is None


@pytest.mark.parametrize(
    ("setting", "env", "reader", "ascii_expected"),
    [
        ("auto", {"LANG": "en_US.UTF-8"}, False, False),
        ("auto", {"LANG": "C"}, False, True),
        ("auto", {"TERM": "linux", "LANG": "en_US.UTF-8"}, False, True),
        ("unicode", {"LANG": "C"}, False, False),
        ("ascii", {"LANG": "en_US.UTF-8"}, False, True),
        ("unicode", {"LANG": "en_US.UTF-8"}, True, True),
    ],
)
def test_glyph_selection(
    setting: str, env: dict[str, str], reader: bool, ascii_expected: bool
) -> None:
    theme, _ = resolve_theme("dark", setting, env=env, screen_reader=reader)
    assert (theme.glyphs is ASCII_GLYPHS) is ascii_expected


def test_a_custom_theme_extends_a_builtin(tmp_path: Path) -> None:
    themes = tmp_path / ".agenthicc" / "themes"
    themes.mkdir(parents=True)
    (themes / "acme.toml").write_text(
        'extends = "light"\n[tokens]\naccent = "#ff00aa"\n', encoding="utf-8"
    )
    theme = load_theme("acme", tmp_path)
    assert theme.palette.accent == "#ff00aa"
    assert theme.palette.muted == BUILTIN_THEMES["light"].palette.muted
    assert theme.light
    assert "acme" in available_themes(tmp_path)


def test_a_misspelled_token_names_file_line_and_token(tmp_path: Path) -> None:
    themes = tmp_path / ".agenthicc" / "themes"
    themes.mkdir(parents=True)
    (themes / "acme.toml").write_text('[tokens]\nacent = "#ff00aa"\n', encoding="utf-8")
    with pytest.raises(ThemeError, match=r"acme\.toml:2: unknown token 'acent'"):
        load_theme("acme", tmp_path)
    theme, notices = resolve_theme("acme", "auto", env={}, cwd=tmp_path)
    assert theme.name == "dark"
    assert notices and "unknown token 'acent'" in notices[0]


def test_an_invalid_colour_is_reported(tmp_path: Path) -> None:
    themes = tmp_path / ".agenthicc" / "themes"
    themes.mkdir(parents=True)
    (themes / "bad.toml").write_text('[tokens]\naccent = "chartreuse-ish"\n', encoding="utf-8")
    with pytest.raises(ThemeError, match="invalid colour"):
        load_theme("bad", tmp_path)


def test_an_unknown_theme_falls_back_with_a_notice() -> None:
    theme, notices = resolve_theme("nope", "auto", env={})
    assert theme.name == "dark" and "unknown theme 'nope'" in notices[0]


def test_no_color_theme_writes_no_colour_sgr() -> None:
    use_theme(BUILTIN_THEMES["no-color"])
    console = Console(force_terminal=True, color_system="truecolor", width=60, record=True)
    p = palette()
    console.print(f"[{p.accent}]a[/] [{p.danger}]b[/] [bold]c[/bold]", style=p.on("user_bg"))
    output = console.export_text(styles=True)
    assert not re.search(r"\x1b\[[0-9;]*(3[0-9]|4[0-9]|9[0-7]|10[0-7])[;m]", output)


def test_theme_module_names_every_public_symbol() -> None:
    for name in theme_module.__all__:
        assert hasattr(theme_module, name), name


def test_save_ui_preference_changes_only_its_line(tmp_path: Path) -> None:
    from agenthicc.tui.preferences import save_ui_preference

    config = tmp_path / "agenthicc.toml"
    config.write_text(
        '# mine\n[execution]\nprovider = "anthropic"\n\n[ui]\nmouse = "off"\n\n[tools]\nx = 1\n',
        encoding="utf-8",
    )
    save_ui_preference("theme", "light", config)
    save_ui_preference("theme", 'so"lar', config)
    text = config.read_text(encoding="utf-8")
    assert text.count("theme =") == 1
    assert '[ui]\nmouse = "off"\ntheme = "so\\"lar"\n' in text
    assert text.startswith("# mine\n[execution]") and text.endswith("[tools]\nx = 1\n")

    import tomllib

    assert tomllib.loads(text)["ui"]["theme"] == 'so"lar'


def test_save_ui_preference_appends_a_ui_table(tmp_path: Path) -> None:
    from agenthicc.tui.preferences import save_ui_preference

    config = tmp_path / "nested" / "agenthicc.toml"
    save_ui_preference("theme", "dark", config)
    assert config.read_text(encoding="utf-8") == '[ui]\ntheme = "dark"\n'
    with pytest.raises(ValueError):
        save_ui_preference("bad key", "x", config)


def test_ui_settings_parse_theme_and_glyphs(tmp_path: Path) -> None:
    from agenthicc.config import UISettings, load_config

    config = tmp_path / "agenthicc.toml"
    config.write_text('[ui]\ntheme = "light"\nglyphs = "ASCII"\n', encoding="utf-8")
    cfg = load_config(config_path=str(config))
    assert cfg.ui.theme == "light" and cfg.ui.glyphs == "ascii"
    with pytest.raises(ValueError, match="ui.glyphs"):
        UISettings(glyphs="emoji")
    with pytest.raises(ValueError, match="ui.theme"):
        UISettings(theme=" ")


def test_theme_command_switches_without_restart(tmp_path: Path) -> None:
    from agenthicc.commands.builtins import BUILTIN_COMMANDS
    from agenthicc.commands.command import CommandContext
    from agenthicc.config import AgenthiccConfig

    (command,) = [cmd for cmd in BUILTIN_COMMANDS if cmd.name == "/theme"]
    console = Console(record=True, width=100)
    use_theme(BUILTIN_THEMES["dark"].with_glyphs(ASCII_GLYPHS))
    assert command.handler is not None
    command.handler(
        CommandContext(
            text="/theme light", args="light", model="m", console=console, config=AgenthiccConfig()
        )
    )
    assert current_theme().name == "light"
    assert current_theme().glyphs is ASCII_GLYPHS  # the glyph choice is kept
    command.handler(
        CommandContext(
            text="/theme nope", args="nope", model="m", console=console, config=AgenthiccConfig()
        )
    )
    assert "unknown theme 'nope'" in console.export_text()
    assert current_theme().name == "light"


def test_theme_picker_previews_applies_and_saves() -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.overlays.theme_picker import ThemePickerOverlay

    applied: list[str] = []
    saved: list[str] = []
    closed: list[bool] = []
    picker = ThemePickerOverlay(
        ("dark", "light", "broken"),
        current="dark",
        load=lambda name: BUILTIN_THEMES[name] if name in BUILTIN_THEMES else load_theme(name),
        apply=lambda theme: applied.append(theme.name),
        save=lambda name: saved.append(name) or f"saved {name}",
        close=lambda: closed.append(True),
    )
    console = Console(record=True, width=80)
    console.print(picker.render())
    assert "fix the flaky scheduler test" in console.export_text()
    picker.handle_key(Key.DOWN, "")
    picker.handle_key(Key.ENTER, "")
    picker.handle_key(Key.CHAR, "s")
    assert applied == ["light"] and saved == ["light"]
    picker.handle_key(Key.DOWN, "")
    picker.handle_key(Key.ENTER, "")
    console = Console(record=True, width=80)
    console.print(picker.render())
    assert "unknown theme 'broken'" in console.export_text()
    picker.handle_key(Key.ESC, "")
    assert closed == [True] and applied == ["light"]


def test_parse_osc_colour_reports() -> None:
    from agenthicc.tui.cbreak_reader import parse_osc_color

    assert parse_osc_color(b"11;rgb:ffff/ffff/ffff") == (255, 255, 255)
    assert parse_osc_color(b"11;rgb:1818/1818/1b1b") == (24, 24, 27)
    assert parse_osc_color(b"11;rgb:f/0/8") == (255, 0, 136)
    assert parse_osc_color(b"11;rgb:zz/00/00") is None
    assert parse_osc_color(b"11;#ffffff") is None
    assert parse_osc_color(b"garbage") is None


def test_a_late_osc_answer_is_not_typed_text() -> None:
    import os

    from agenthicc.tui.cbreak_reader import Key, read_key

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\a")
        assert read_key(read_fd) == (Key.CHAR, "a")
        os.write(write_fd, b"\x1b]11;rgb:0/0/0\x07b")
        assert read_key(read_fd) == (Key.CHAR, "b")
        os.write(write_fd, b"\x1b]")
        assert read_key(read_fd) == (Key.ALT, "]")
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_the_background_query_is_skipped_without_a_terminal() -> None:
    import os

    from agenthicc.tui.cbreak_reader import query_background_color

    read_fd, write_fd = os.pipe()
    try:
        assert query_background_color(read_fd, 0.01) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_diff_rendering_follows_the_active_theme() -> None:
    from agenthicc.tui.diff_renderer import render_file_diff

    def rendered(name: str) -> str:
        use_theme(BUILTIN_THEMES[name])
        console = Console(force_terminal=True, color_system="truecolor", width=100, record=True)
        console.print(render_file_diff("a.py", ["x = 1"], ["x = 2"]))
        return console.export_text(styles=True)

    dark, light = rendered("dark"), rendered("light")
    assert "48;2;21;58;21" in dark  # dark diff_add_bg #153a15
    assert "48;2;230;244;231" in light  # light diff_add_bg #e6f4e7
    assert dark != light
