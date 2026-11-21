"""TUX-08 §2.4 / AC 7: key help reads in phrases, never in internal condition flags."""

from __future__ import annotations

import re

import pytest
from rich.console import Console

from agenthicc.tui.actions import DEFAULT_ACTIONS
from agenthicc.tui.keymap import CONTEXTS, Keymap
from agenthicc.tui.keymap_phrases import CONDITION_PHRASES, describe_conditions, phrase
from agenthicc.tui.workspace.agents_hub.render import help_lines

pytestmark = pytest.mark.unit

#: An internal identifier (``library_tab``) or a raw negation (``not session``).
_IDENTIFIER = re.compile(r"\b[a-z]+_[a-z_]+\b")
_NEGATION = re.compile(r"\bnot [a-z_]+\b")


def test_every_condition_in_the_default_actions_has_a_phrase() -> None:
    """A new condition fails here, so the help screen never falls back to a flag name."""
    used = {flag for action in DEFAULT_ACTIONS for flag in (*action.when, *action.unless)}
    assert used, "the default actions use no conditions"
    assert sorted(used - set(CONDITION_PHRASES)) == []
    assert sorted(set(CONDITION_PHRASES) - used) == [], "a phrase for a condition nobody uses"


def test_phrases_are_words_and_negations_read_except() -> None:
    for flag, text in CONDITION_PHRASES.items():
        assert text and _IDENTIFIER.search(text) is None, (flag, text)
        assert not text.startswith(("not ", "except ")), (flag, text)
    assert describe_conditions({"library_tab"}, {"needs_approval"}) == (
        "in Library, except when an agent needs approval"
    )
    assert describe_conditions((), ()) == ""
    assert phrase("editable_definition") == "on your own agent definitions"


def test_an_unknown_condition_still_reads_as_words() -> None:
    assert phrase("future_flag") == "when future flag"


@pytest.mark.parametrize("context", CONTEXTS)
def test_no_help_entry_shows_a_condition_identifier(context: str) -> None:
    keymap = Keymap.build(DEFAULT_ACTIONS)
    for entry in keymap.help(context):
        assert _IDENTIFIER.search(entry.conditions) is None, (context, entry)
        assert _NEGATION.search(entry.conditions) is None, (context, entry)


@pytest.mark.parametrize("context", ["agents", "agents.list", "agents.peek", "agents.detail"])
def test_the_hub_help_overlay_text_has_no_identifier(context: str) -> None:
    keymap = Keymap.build(DEFAULT_ACTIONS)
    console = Console(record=True, width=240, color_system=None, force_terminal=False)
    for line in help_lines("Agents", keymap.help(context), 240):
        console.print(line)
    text = console.export_text()
    conditions = re.findall(r"\(([^)]*)\)", text)
    assert conditions, text
    for found in conditions:
        assert _IDENTIFIER.search(found) is None, found
        assert _NEGATION.search(found) is None, found
