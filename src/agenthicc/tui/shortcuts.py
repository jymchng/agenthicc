"""Every key in every context, as a page (TUX-10 §2.6).

:func:`shortcut_groups` turns the effective keymap (the defaults plus the
user's ``keybindings.toml``) into the rows ``/shortcuts`` and ``?`` show,
grouped as the user thinks of the screens: the composer, the pickers, the
dialogs, the transcript view and the agents hub. The text of a row comes from
the same :class:`~agenthicc.tui.keymap.Action` entries as the footer hints and
the hub's help, so the three cannot disagree, and the conditions it applies
under read as words (TUX-08), never as flag names.
"""

from __future__ import annotations

from dataclasses import dataclass

from agenthicc.tui.keymap import CONTEXTS, Keymap, display_key
from agenthicc.tui.keymap_phrases import describe_conditions

__all__ = [
    "CONTEXT_TITLES",
    "GROUPS",
    "ShortcutGroup",
    "ShortcutRow",
    "ShortcutSection",
    "shortcut_groups",
]

#: The page's groups and the keymap contexts each one lists, in order.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Composer", ("composer", "search", "queue", "subagents")),
    ("Pickers", ("picker",)),
    ("Dialogs", ("dialog",)),
    ("Transcript view", ("transcript_view", "transcript")),
    ("Agents hub", ("agents", "agents.list", "agents.peek", "agents.detail")),
)

#: A context's own heading inside its group; ``""`` for the group's main context.
CONTEXT_TITLES: dict[str, str] = {
    "composer": "",
    "search": "While searching your prompts",
    "queue": "In the queued messages",
    "subagents": "In the subagent panel",
    "picker": "",
    "dialog": "",
    "transcript_view": "",
    "transcript": "In a worker's transcript",
    "agents": "On any agent",
    "agents.list": "In the agent list",
    "agents.peek": "In the agent preview",
    "agents.detail": "In an agent's detail",
    "global": "Everywhere",
    "overlay": "In an overlay",
}

#: Where a binding that is not the default comes from, as the page says it.
USER_SOURCE = "your keybindings"
DEFAULT_SOURCE = "default"


@dataclass(frozen=True)
class ShortcutRow:
    """One key binding: the action, its keys and what it does."""

    action: str
    keys: str
    description: str
    #: When it applies, in words (``while a turn runs``); ``""`` for always.
    conditions: str
    #: ``default`` or ``your keybindings``.
    source: str


@dataclass(frozen=True)
class ShortcutSection:
    """The rows of one keymap context, under its heading."""

    context: str
    title: str
    rows: tuple[ShortcutRow, ...]


@dataclass(frozen=True)
class ShortcutGroup:
    """One screen's keys: ``Composer``, ``Dialogs``…"""

    title: str
    sections: tuple[ShortcutSection, ...]

    @property
    def rows(self) -> tuple[ShortcutRow, ...]:
        return tuple(row for section in self.sections for row in section.rows)


def _row(keymap: Keymap, action_name: str) -> ShortcutRow:
    action = keymap.action(action_name)
    keys = " / ".join(display_key(key, leader=keymap.leader) for key in action.keys) or "unbound"
    source = keymap.sources.get(action.name, DEFAULT_SOURCE)
    return ShortcutRow(
        action=action.name,
        keys=keys,
        description=action.description or action.label,
        conditions=describe_conditions(action.when, action.unless),
        source=DEFAULT_SOURCE if source == DEFAULT_SOURCE else USER_SOURCE,
    )


def shortcut_groups(keymap: Keymap) -> tuple[ShortcutGroup, ...]:
    """Every action of *keymap*, once, in its context's group.

    A context that no group names (a plugin's, ``global``) is listed last under
    its own title, so a new context never disappears from the page.
    """
    by_context: dict[str, list[str]] = {}
    for action in keymap.actions:
        by_context.setdefault(action.context, []).append(action.name)
    placed = {context for _, contexts in GROUPS for context in contexts}
    groups: list[ShortcutGroup] = []
    for title, contexts in GROUPS:
        sections = tuple(
            ShortcutSection(
                context, CONTEXT_TITLES.get(context, context), _rows(keymap, by_context, context)
            )
            for context in contexts
            if by_context.get(context)
        )
        if sections:
            groups.append(ShortcutGroup(title, sections))
    leftover = tuple(
        ShortcutSection(
            context, CONTEXT_TITLES.get(context, context), _rows(keymap, by_context, context)
        )
        for context in (*CONTEXTS, *sorted(set(by_context) - set(CONTEXTS)))
        if context not in placed and by_context.get(context)
    )
    if leftover:
        groups.append(ShortcutGroup("Other", leftover))
    return tuple(groups)


def _rows(
    keymap: Keymap, by_context: dict[str, list[str]], context: str
) -> tuple[ShortcutRow, ...]:
    return tuple(_row(keymap, name) for name in by_context[context])
