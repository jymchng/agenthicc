"""What the empty composer suggests (TUX-03 §2.5).

The placeholder is chosen by context, most specific first:

1. ``AGENTS.md`` is missing from this repository: ``try /init …``;
2. an interrupted session exists for the directory: ``resume … /resume``;
3. background sessions need the user: ``2 sessions need you — press ←``;
4. otherwise a tip, one of :data:`TIPS`, rotating by session.

The first, second and last are *static*: :func:`static_suggestion` decides them
once, off the event loop, and the session stores the result in
``ConversationStore.suggestion``. The third is live, so :func:`current_suggestion`
looks at the attention count each time the composer is drawn. A tip changes
once per session, never while it is visible.

A suggestion that is a command carries it; Tab on the empty prompt puts that
command in the composer (``composer.accept_suggestion``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agenthicc.tui.keymap import Keymap

__all__ = [
    "TIPS",
    "Suggestion",
    "Tip",
    "agents_md_missing",
    "current_suggestion",
    "static_suggestion",
    "tip_for",
]

SuggestionKind = Literal["init", "resume", "attention", "tip"]


@dataclass(frozen=True, slots=True)
class Suggestion:
    """The text shown in the empty composer, and the command Tab accepts."""

    kind: SuggestionKind
    text: str
    #: The slash command Tab puts in the prompt; ``""`` when the text is not one.
    command: str = ""


@dataclass(frozen=True, slots=True)
class Tip:
    """One tip: a sentence naming a feature with its key or its command.

    ``{key}`` is replaced by the first key the user's keymap binds to
    *action*, so a rebound key is the one the tip names; a tip whose action is
    unbound is skipped.
    """

    text: str
    action: str = ""
    command: str = ""


#: Twenty tips. Ten name a key (read from the keymap), ten a slash command.
TIPS: tuple[Tip, ...] = (
    Tip("{key} adds a file", "composer.mention"),
    Tip("{key} searches history", "composer.history_search"),
    Tip("{key} cycles mode", "composer.mode_cycle"),
    Tip("{key} opens the agents hub", "agents.open_hub"),
    Tip("{key} switches to the next agent", "agents.cycle"),
    Tip("{key} opens the prompt in your editor", "composer.external_editor"),
    Tip("{key} adds a line break", "composer.newline"),
    Tip("{key} undoes your last edit", "composer.undo"),
    Tip("{key} pauses the running turn", "composer.interrupt"),
    Tip("{key} sends the running subagents to the background", "subagents.background"),
    Tip("/compact summarises the conversation to free up context", command="/compact"),
    Tip("/theme changes the colours without a restart", command="/theme"),
    Tip("/status shows the session, model and context use", command="/status"),
    Tip("/details sets how much of each tool call you see", command="/details"),
    Tip("/model switches provider or model", command="/model"),
    Tip("/workflow lists the workflows you can run", command="/workflow"),
    Tip("/loop repeats a prompt on a schedule", command="/loop"),
    Tip("/keybindings shows and edits your key bindings", command="/keybindings"),
    Tip("/help lists every command", command="/help"),
    Tip("/release-notes shows what changed in this version", command="/release-notes"),
)

INIT_TEXT = "try /init to write AGENTS.md for this repository"
RESUME_TEXT = "resume your last session: /resume"


def agents_md_missing(cwd: Path) -> bool:
    """Whether *cwd* is in a repository none of whose directories up to its root has ``AGENTS.md``.

    Outside a repository there is nothing to initialise, so nothing is missing.
    """
    from agenthicc.tui.statusline import repository_root

    root = repository_root(cwd)
    if root is None:
        return False
    for directory in (cwd, *cwd.parents):
        if (directory / "AGENTS.md").is_file():
            return False
        if directory == root:
            break
    return True


def tip_for(index: int, keymap: Keymap) -> Suggestion:
    """The *index*-th tip (modulo the list), skipping any whose key is unbound."""
    from agenthicc.tui.keymap import KeymapError, display_key

    for offset in range(len(TIPS)):
        tip = TIPS[(index + offset) % len(TIPS)]
        text = tip.text
        if tip.action:
            try:
                keys = keymap.action(tip.action).keys
            except KeymapError:
                continue
            if not keys:
                continue
            text = text.format(key=display_key(keys[0], leader=keymap.leader))
        return Suggestion("tip", text, tip.command)
    return Suggestion("tip", TIPS[-1].text, TIPS[-1].command)


def static_suggestion(cwd: Path, *, session_id: str, tip_index: int, keymap: Keymap) -> Suggestion:
    """The suggestion that does not change while the session runs (reads the disk)."""
    from agenthicc.tui.runtime.session_log import find_interrupted_session

    if agents_md_missing(cwd):
        return Suggestion("init", INIT_TEXT, "/init")
    if find_interrupted_session(str(cwd), exclude=session_id) is not None:
        return Suggestion("resume", RESUME_TEXT, "/resume")
    return tip_for(tip_index, keymap)


def current_suggestion(
    static: Suggestion | None, needs_you: int, hub_key: str = ""
) -> Suggestion | None:
    """What the empty composer shows now: the *static* suggestion, or the attention one.

    *needs_you* is how many background sessions need the user, and *hub_key*
    the key that opens the agents hub (``←``), named in the attention text.
    ``None`` until the session has decided its static suggestion.
    """
    if static is not None and static.kind in ("init", "resume"):
        return static
    if needs_you > 0:
        from agenthicc.tui.attention_labels import sessions_need_you
        from agenthicc.tui.theme import glyphs

        press = f" {glyphs().sep} press {hub_key}" if hub_key else ""
        return Suggestion("attention", f"{sessions_need_you(needs_you)}{press}")
    return static
