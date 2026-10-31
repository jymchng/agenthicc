"""The key-action registry: names, conflicts, conditions, hints, the bindings file (AGX-13)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from agenthicc.tui.actions import COMPOSER_ACTIONS, DEFAULT_ACTIONS
from agenthicc.tui.keymap import (
    Action,
    Dispatcher,
    Keymap,
    KeymapError,
    display_key,
    load_keybindings,
    normalize_key,
    template,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("written", "canonical"),
    [
        ("y", "y"),
        ("Y", "Y"),
        ("shift+y", "Y"),
        ("Ctrl+Y", "ctrl+y"),
        ("alt+X", "alt+shift+x"),
        ("Shift+Tab", "shift+tab"),
        ("ctrl+enter", "ctrl+j"),
        ("esc", "escape"),
        ("PgDn", "pagedown"),
        ("F5", "f5"),
        ("shift+ctrl+left", "ctrl+shift+left"),
        ("<leader>a", "<leader>a"),
        (" ", "space"),
    ],
)
def test_bindings_normalize(written: str, canonical: str) -> None:
    assert normalize_key(written) == canonical


@pytest.mark.parametrize("written", ["", "hyper+x", "ctrl+banana", "<leader>"])
def test_invalid_bindings_are_rejected(written: str) -> None:
    with pytest.raises(KeymapError):
        normalize_key(written)


def test_keys_display_the_way_hints_show_them() -> None:
    assert display_key("ctrl+g") == "Ctrl+G"
    assert display_key("escape") == "Esc"
    assert display_key("shift+tab") == "Shift+Tab"
    assert display_key("<leader>a", leader="ctrl+x") == "Ctrl+X a"
    assert display_key("f12") == "F12"


def _action(name: str, keys: tuple[str, ...], **extra: object) -> Action:
    return Action(name, "composer", keys, name.split(".")[-1], **extra)  # type: ignore[arg-type]


def test_two_actions_may_share_a_key_only_under_exclusive_conditions() -> None:
    with pytest.raises(KeymapError, match="bound to both"):
        Keymap.build([_action("composer.a", ("x",)), _action("composer.b", ("x",))])
    with pytest.raises(KeymapError, match="bound to both"):
        Keymap.build(
            [
                _action("composer.a", ("x",), when=frozenset({"running"})),
                _action("composer.b", ("x",), when=frozenset({"paste"})),
            ]
        )
    keymap = Keymap.build(
        [
            _action("composer.a", ("x",), when=frozenset({"running"})),
            _action("composer.b", ("x",), unless=frozenset({"running"})),
        ]
    )
    assert keymap.resolve("composer", "x", frozenset({"running"})).name == "composer.a"  # type: ignore[union-attr]
    assert keymap.resolve("composer", "x").name == "composer.b"  # type: ignore[union-attr]


def test_definitions_are_validated() -> None:
    with pytest.raises(KeymapError, match="unknown context"):
        Keymap.build([Action("x.y", "nowhere", ("a",), "y")])
    with pytest.raises(KeymapError, match="defined twice"):
        Keymap.build([_action("composer.a", ("a",)), _action("composer.a", ("b",))])
    with pytest.raises(KeymapError, match="normalized"):
        Keymap.build([_action("composer.a", ("Ctrl+A",))])
    with pytest.raises(KeymapError, match="without a leader"):
        Keymap.build([_action("composer.a", ("<leader>a",))])
    with pytest.raises(KeymapError, match="leader key"):
        Keymap.build([_action("composer.a", ("ctrl+x",))], leader="ctrl+x")


def test_the_defaults_have_no_conflicts() -> None:
    keymap = Keymap.build(DEFAULT_ACTIONS)
    assert {action.name for action in keymap.actions} == {a.name for a in DEFAULT_ACTIONS}


def test_hints_are_the_applicable_actions_by_priority() -> None:
    keymap = Keymap.build(COMPOSER_ACTIONS)
    idle = [hint.label for hint in keymap.hints("composer")]
    assert idle[:4] == ["submit", "newline", "commands", "mention"]
    assert "pause" not in idle
    running = [hint.label for hint in keymap.hints("composer", frozenset({"running"}))]
    assert "pause" in running and "cancel" in running
    background = [
        hint.label
        for hint in keymap.hints(
            "composer",
            frozenset({"running", "subagents_running"}),
        )
    ]
    assert "background pool" in background
    assert "background pool" not in running
    pasted = keymap.hints("composer", frozenset({"paste", "paste_at_cursor"}))
    assert [hint.label for hint in pasted][:2] == ["expand paste", "delete paste"]
    assert pasted[0].keys == "Ctrl+V"


def test_help_lists_every_action_with_its_conditions() -> None:
    entries = {entry.action: entry for entry in Keymap.build(COMPOSER_ACTIONS).help("composer")}
    assert set(entries) == {action.name for action in COMPOSER_ACTIONS}
    assert entries["composer.interrupt"].keys == "Esc"
    assert entries["composer.interrupt"].conditions == "running, not paste_at_cursor"
    assert entries["composer.submit"].source == "default"


def test_a_leader_key_starts_a_two_step_binding() -> None:
    now = [100.0]
    keymap = Keymap.build(
        [_action("composer.hub", ("<leader>a",)), _action("composer.plain", ("a",))],
        leader="ctrl+x",
    )
    dispatcher = Dispatcher(keymap, clock=lambda: now[0])
    assert dispatcher.dispatch("composer", "ctrl+x") is None
    assert dispatcher.awaiting_leader
    assert dispatcher.dispatch("composer", "a").name == "composer.hub"  # type: ignore[union-attr]
    # After the timeout the second key is an ordinary key again.
    dispatcher.dispatch("composer", "ctrl+x")
    now[0] += 5.0
    assert dispatcher.dispatch("composer", "a").name == "composer.plain"  # type: ignore[union-attr]


# ── the bindings file ────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_no_file_means_the_defaults(tmp_path: Path) -> None:
    loaded = load_keybindings(DEFAULT_ACTIONS, tmp_path / "keybindings.toml")
    assert loaded.errors == ()
    assert loaded.keymap.action("composer.newline").keys == ("ctrl+j",)


def test_a_file_rebinds_adds_and_removes_keys(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "keybindings.toml",
        '[composer]\n"composer.newline" = ["alt+enter", "ctrl+j"]\n"composer.clear" = "none"\n',
    )
    loaded = load_keybindings(DEFAULT_ACTIONS, path)
    assert loaded.errors == ()
    assert loaded.keymap.action("composer.newline").keys == ("alt+enter", "ctrl+j")
    assert loaded.keymap.action("composer.clear").keys == ()
    assert loaded.keymap.sources["composer.newline"] == str(path)
    assert loaded.keymap.resolve("composer", "ctrl+u") is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[composer]\n"composer.nothing" = "x"\n', "no such action"),
        ('[nowhere]\n"composer.submit" = "x"\n', "unknown context"),
        ('[composer]\n"composer.clear" = "enter"\n', "bound to both"),
        ('[composer]\n"composer.commands" = "!"\n', "cannot be rebound"),
        ('[composer]\n"composer.clear" = "ctrl+banana"\n', "not a key"),
        ("[composer\n", ""),
    ],
)
def test_an_invalid_file_is_reported_and_the_defaults_apply(
    tmp_path: Path, text: str, message: str
) -> None:
    loaded = load_keybindings(DEFAULT_ACTIONS, _write(tmp_path / "keybindings.toml", text))
    assert loaded.errors
    assert message in " ".join(loaded.errors)
    assert loaded.keymap.action("composer.clear").keys == ("ctrl+u",)


def test_the_leader_is_read_from_the_file(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "keybindings.toml",
        'leader = "ctrl+t"\nleader_timeout_ms = 1500\n[composer]\n"composer.clear" = "<leader>u"\n',
    )
    loaded = load_keybindings(DEFAULT_ACTIONS, path)
    assert loaded.errors == ()
    assert loaded.keymap.leader == "ctrl+t"
    assert loaded.keymap.leader_timeout_s == 1.5


def test_the_template_lists_every_rebindable_action_and_loads_as_defaults(tmp_path: Path) -> None:
    text = template(DEFAULT_ACTIONS)
    tomllib.loads(text)
    for action in DEFAULT_ACTIONS:
        assert (f'"{action.name}"' in text) is action.rebindable
    assert load_keybindings(DEFAULT_ACTIONS, _write(tmp_path / "k.toml", text)).errors == ()
