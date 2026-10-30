"""The default key actions of every screen (AGX-13).

Each screen's dispatch, footer hints and help come from these entries through
:class:`agenthicc.tui.keymap.Keymap`. Conditions are the flags a screen reports
about its state; the composer reports:

- ``running`` — a turn is in progress (input is queued);
- ``paste`` — a large paste is shown condensed;
- ``paste_at_cursor`` — the cursor is right after that condensed paste.

The agents hub (AGX-05–07) reports, for its selected row and tab:

- ``row`` — a row is selected; ``agent`` — it is a background agent or one of
  its inbox requests; ``session`` — it is a background agent;
  ``inbox`` — it is an inbox request; ``saved`` — a saved foreground session;
- ``needs_approval``, ``accepts_input``, ``resumable``, ``retryable``,
  ``stoppable``, ``archivable``, ``attachable``, ``pinned``, ``worker`` —
  what the selected agent's state allows;
- ``collapsed`` / ``expanded`` — the selected row is a tree with hidden or
  shown children; ``running_tab`` — the Running tab is shown;
- ``transcript``, ``changes`` — the detail view has those sections.

The composer also reports ``subagents`` while the subagent panel is shown
(AGX-08), and ``subagents_running`` while a pool has live workers (AGX-08
Phase 2). The panel and a worker's transcript report ``worker`` (the selected
worker has a transcript), ``running_worker`` (it has not finished) and
``failed_worker`` (it failed and its row has not been dismissed).
"""

from __future__ import annotations

from agenthicc.tui.keymap import Action

__all__ = [
    "AGENTS_ACTIONS",
    "AGENTS_DETAIL_ACTIONS",
    "AGENTS_LIST_ACTIONS",
    "AGENTS_PEEK_ACTIONS",
    "COMPOSER_ACTIONS",
    "DEFAULT_ACTIONS",
    "GLOBAL_ACTIONS",
    "SUBAGENTS_ACTIONS",
    "TRANSCRIPT_ACTIONS",
]

_RUNNING = frozenset({"running"})
_PASTE = frozenset({"paste"})
_PASTE_AT_CURSOR = frozenset({"paste_at_cursor"})

GLOBAL_ACTIONS: tuple[Action, ...] = ()

COMPOSER_ACTIONS: tuple[Action, ...] = (
    Action(
        "composer.submit",
        "composer",
        ("enter",),
        "submit",
        hint_priority=90,
        description="Send the prompt; while a turn runs it is queued",
    ),
    Action(
        "composer.newline",
        "composer",
        ("ctrl+j",),
        "newline",
        hint_priority=80,
        description="Insert a line break",
    ),
    Action(
        "composer.commands",
        "composer",
        ("/",),
        "commands",
        hint_priority=75,
        description="Open the command picker at the start of a prompt",
        rebindable=False,
    ),
    Action(
        "composer.mention",
        "composer",
        ("@",),
        "mention",
        hint_priority=74,
        description="Mention a file or resource",
        rebindable=False,
    ),
    Action(
        "composer.expand_paste",
        "composer",
        ("ctrl+v",),
        "expand paste",
        hint_priority=96,
        when=_PASTE,
        description="Show a condensed paste in full",
    ),
    Action(
        "composer.drop_paste",
        "composer",
        ("escape",),
        "delete paste",
        hint_priority=95,
        when=_PASTE_AT_CURSOR,
        description="Delete the condensed paste before the cursor",
    ),
    Action(
        "composer.interrupt",
        "composer",
        ("escape",),
        "pause",
        hint_priority=70,
        when=_RUNNING,
        unless=_PASTE_AT_CURSOR,
        description="Pause the running turn",
    ),
    Action(
        "composer.cancel",
        "composer",
        ("ctrl+c",),
        "cancel",
        hint_priority=60,
        when=_RUNNING,
        description="Cancel the running turn",
    ),
    Action(
        "composer.quit",
        "composer",
        ("ctrl+c",),
        "exit",
        unless=_RUNNING,
        description="Exit agenthicc (press twice)",
    ),
    Action(
        "composer.eof",
        "composer",
        ("ctrl+d",),
        "submit or exit",
        unless=_RUNNING,
        description="Send a non-empty prompt, or exit on an empty one",
    ),
    Action(
        "composer.delete_back",
        "composer",
        ("backspace",),
        "delete",
        description="Delete the character (or condensed paste) before the cursor",
    ),
    Action("composer.clear", "composer", ("ctrl+u",), "clear", description="Clear the prompt"),
    Action("composer.left", "composer", ("left",), "left", description="Move the cursor left"),
    Action("composer.right", "composer", ("right",), "right", description="Move the cursor right"),
    Action("composer.home", "composer", ("home",), "home", description="Move to the line start"),
    Action("composer.end", "composer", ("end",), "end", description="Move to the line end"),
    Action(
        "composer.history_previous",
        "composer",
        ("up",),
        "previous",
        unless=_RUNNING,
        description="Move up a line, or recall the previous prompt",
    ),
    Action(
        "composer.history_next",
        "composer",
        ("down",),
        "next",
        unless=_RUNNING,
        description="Move down a line, or recall the next prompt",
    ),
    Action(
        "composer.agent_cycle",
        "composer",
        ("tab",),
        "agent",
        hint_priority=35,
        description="Talk to the next primary agent",
    ),
    Action(
        "composer.mode_cycle",
        "composer",
        ("shift+tab",),
        "mode",
        description="Cycle Safe → Plan → Yolo",
    ),
    Action(
        "agents.open_hub",
        "composer",
        ("ctrl+g", "left_on_empty"),
        "agents",
        hint_priority=40,
        description="Open the agents hub (← only on an empty prompt)",
    ),
    Action(
        "subagents.focus",
        "composer",
        ("ctrl+o",),
        "subagents",
        hint_priority=45,
        description="Move to the subagent panel",
        when=frozenset({"subagents"}),
    ),
    Action(
        "subagents.background",
        "composer",
        ("ctrl+b",),
        "background pool",
        hint_priority=46,
        description="Let the active subagent pool continue in the background",
        when=frozenset({"running", "subagents_running"}),
    ),
)


def _action(
    context: str,
    name: str,
    keys: tuple[str, ...],
    label: str,
    description: str,
    *,
    priority: int = 0,
    when: frozenset[str] = frozenset(),
    paired: str = "",
) -> Action:
    return Action(
        f"agents.{name}",
        context,
        keys,
        label,
        hint_priority=priority,
        description=description,
        when=when,
        paired=paired,
    )


def _flag(*names: str) -> frozenset[str]:
    return frozenset(names)


#: Operations on the selected agent: on the list, in the peek panel and in the
#: detail view alike.
AGENTS_ACTIONS: tuple[Action, ...] = (
    _action(
        "agents",
        "approve",
        ("y",),
        "approve",
        "Approve the exact pending request",
        priority=99,
        when=_flag("needs_approval"),
    ),
    _action(
        "agents",
        "reject",
        ("n",),
        "reject",
        "Reject the exact pending request",
        priority=98,
        when=_flag("needs_approval"),
    ),
    _action(
        "agents",
        "input",
        ("i",),
        "reply",
        "Answer the agent's question or send it input",
        priority=96,
        when=_flag("accepts_input"),
    ),
    _action(
        "agents",
        "resume",
        ("r",),
        "resume",
        "Resume the agent in the background",
        priority=90,
        when=_flag("resumable"),
    ),
    _action(
        "agents",
        "retry",
        ("R",),
        "retry",
        "Retry the agent from scratch",
        priority=70,
        when=_flag("retryable"),
    ),
    _action(
        "agents",
        "stop",
        ("c",),
        "stop",
        "Stop the agent (or every marked agent)",
        priority=65,
        when=_flag("stoppable"),
    ),
    _action(
        "agents",
        "archive",
        ("A",),
        "archive",
        "Archive the finished agent (or every marked agent)",
        priority=30,
        when=_flag("archivable"),
    ),
    _action(
        "agents",
        "pin",
        ("p",),
        "pin",
        "Pin or unpin the agent",
        priority=20,
        when=_flag("session"),
    ),
    _action("agents", "rename", ("e",), "rename", "Rename the agent", when=_flag("session")),
    _action("agents", "labels", ("l",), "labels", "Set the agent's labels", when=_flag("session")),
    _action(
        "agents",
        "priority",
        ("P",),
        "priority",
        "Change the agent's queue priority",
        when=_flag("session"),
    ),
    _action(
        "agents",
        "delete",
        ("ctrl+x",),
        "delete",
        "Move the agent (or every marked agent) to recoverable trash",
        priority=5,
        when=_flag("session"),
    ),
    _action("agents", "help", ("?",), "help", "Show every key"),
    _action("agents", "quit", ("q", "escape"), "quit", "Close the agents hub"),
)

AGENTS_LIST_ACTIONS: tuple[Action, ...] = (
    _action("agents.list", "up", ("up", "k"), "up", "Select the previous row"),
    _action(
        "agents.list",
        "down",
        ("down", "j"),
        "select",
        "Select the next row",
        priority=100,
        paired="agents.up",
    ),
    _action("agents.list", "page_up", ("pageup",), "page up", "Move up a screen"),
    _action("agents.list", "page_down", ("pagedown",), "page down", "Move down a screen"),
    _action("agents.list", "first", ("home",), "first", "Select the first row"),
    _action("agents.list", "last", ("end",), "last", "Select the last row"),
    _action(
        "agents.list", "group_previous", ("alt+up",), "previous group", "Jump to the previous group"
    ),
    _action("agents.list", "group_next", ("alt+down",), "next group", "Jump to the next group"),
    _action(
        "agents.list",
        "open",
        ("enter",),
        "open",
        "Open the selected row",
        priority=92,
        when=_flag("row"),
    ),
    _action(
        "agents.list",
        "peek",
        ("space",),
        "peek",
        "Show the agent's request or progress below its row",
        priority=85,
        when=_flag("agent"),
    ),
    _action(
        "agents.list",
        "mark",
        ("v", "m"),
        "mark",
        "Mark the agent for stop, archive or delete",
        when=_flag("session"),
    ),
    _action(
        "agents.list",
        "expire",
        ("x",),
        "expire",
        "Expire the pending request",
        priority=40,
        when=_flag("inbox"),
    ),
    _action(
        "agents.list",
        "expand",
        ("right",),
        "expand",
        "Show the agent's workers",
        when=_flag("collapsed"),
    ),
    _action(
        "agents.list",
        "collapse",
        ("left",),
        "collapse",
        "Hide the agent's workers",
        when=_flag("expanded"),
    ),
    _action(
        "agents.list",
        "move_up",
        ("shift+up",),
        "move up",
        "Move the pinned agent up",
        when=_flag("pinned"),
    ),
    _action(
        "agents.list",
        "move_down",
        ("shift+down",),
        "move down",
        "Move the pinned agent down",
        when=_flag("pinned"),
    ),
    _action("agents.list", "search", ("/",), "search", "Search this tab", priority=60),
    _action(
        "agents.list",
        "sort",
        ("s",),
        "sort",
        "Change the order within the Running tab's groups",
        priority=45,
        when=_flag("running_tab"),
    ),
    _action(
        "agents.list",
        "group",
        ("g",),
        "group",
        "Group the Running tab by status or project",
        priority=44,
        when=_flag("running_tab"),
    ),
    _action("agents.list", "next_tab", ("tab",), "next tab", "Show the next tab"),
    _action("agents.list", "previous_tab", ("shift+tab",), "previous tab", "Show the previous tab"),
    _action("agents.list", "tab_running", ("1",), "running", "Show the Running tab"),
    _action("agents.list", "tab_needs_you", ("2",), "needs you", "Show the Needs you tab"),
    _action("agents.list", "tab_history", ("3",), "history", "Show the History tab"),
    _action("agents.list", "tab_library", ("4",), "library", "Show the Library tab"),
    _action(
        "agents.list",
        "dispatch",
        ("+",),
        "new agent",
        "Start a new background agent",
        priority=55,
        when=_flag("running_tab"),
    ),
    _action(
        "agents.list", "refresh", ("ctrl+r", "f5"), "refresh", "Reload the list and check workers"
    ),
    _action("agents.list", "pause", ("z",), "pause", "Pause or resume live updates"),
)

AGENTS_PEEK_ACTIONS: tuple[Action, ...] = (
    _action(
        "agents.peek",
        "close_peek",
        ("space", "escape"),
        "close",
        "Close the peek panel",
        priority=50,
    ),
)

AGENTS_DETAIL_ACTIONS: tuple[Action, ...] = (
    _action(
        "agents.detail",
        "attach",
        ("enter",),
        "attach",
        "Continue the agent in the foreground",
        priority=93,
        when=_flag("attachable"),
    ),
    _action("agents.detail", "back", ("escape",), "back", "Return to the list", priority=1),
    _action("agents.detail", "scroll_up", ("up", "k"), "scroll up", "Scroll up a line"),
    _action("agents.detail", "scroll_down", ("down", "j"), "scroll down", "Scroll down a line"),
    _action("agents.detail", "scroll_page_up", ("pageup", "["), "page up", "Scroll up a screen"),
    _action(
        "agents.detail", "scroll_page_down", ("pagedown", "]"), "page down", "Scroll down a screen"
    ),
    _action("agents.detail", "scroll_top", ("home",), "top", "Scroll to the top"),
    _action("agents.detail", "scroll_bottom", ("end",), "bottom", "Scroll to the bottom"),
    _action(
        "agents.detail",
        "follow",
        ("f",),
        "follow",
        "Follow new transcript events",
        priority=35,
        when=_flag("transcript"),
    ),
    _action(
        "agents.detail",
        "transcript",
        ("t",),
        "transcript",
        "Show the full transcript",
        priority=34,
        when=_flag("transcript"),
    ),
    _action(
        "agents.detail",
        "diff",
        ("d",),
        "diff",
        "Show the full diff of the agent's changes",
        priority=33,
        when=_flag("changes"),
    ),
    _action(
        "agents.detail",
        "open_request",
        ("o",),
        "show in transcript",
        "Scroll the transcript to the pending request",
        priority=36,
        when=_flag("needs_approval"),
    ),
    _action(
        "agents.detail",
        "environment",
        ("x",),
        "actions",
        "Run a reviewed worktree environment action",
        priority=32,
        when=_flag("worker"),
    ),
)

_WORKER = frozenset({"worker"})
_RUNNING_WORKER = frozenset({"running_worker"})
_FAILED_WORKER = frozenset({"failed_worker"})

#: The subagent panel with focus (Ctrl+O) and ``/subagents`` (AGX-08 §5.2).
SUBAGENTS_ACTIONS: tuple[Action, ...] = (
    Action(
        "subagents.up", "subagents", ("up", "k"), "up", description="Select the previous worker"
    ),
    Action(
        "subagents.down",
        "subagents",
        ("down", "j"),
        "select",
        hint_priority=100,
        description="Select the next worker",
        paired="subagents.up",
    ),
    Action(
        "subagents.open",
        "subagents",
        ("enter",),
        "open",
        hint_priority=90,
        description="Open the worker's transcript",
        when=_WORKER,
    ),
    Action(
        "subagents.stop",
        "subagents",
        ("x",),
        "stop",
        hint_priority=60,
        description="Stop the worker; the others go on",
        when=_RUNNING_WORKER,
        unless=_FAILED_WORKER,
    ),
    Action(
        "subagents.dismiss",
        "subagents",
        ("x",),
        "dismiss",
        hint_priority=55,
        description="Remove the failed worker's row",
        when=_FAILED_WORKER,
        unless=_RUNNING_WORKER,
    ),
    Action(
        "subagents.back",
        "subagents",
        ("escape",),
        "back",
        hint_priority=1,
        description="Return to the prompt",
    ),
)

#: One worker's transcript (AGX-08 §5.2).
TRANSCRIPT_ACTIONS: tuple[Action, ...] = (
    Action(
        "transcript.up",
        "transcript",
        ("up", "k"),
        "scroll up",
        description="Scroll up a line; at the top, return to the conversation",
    ),
    Action(
        "transcript.down",
        "transcript",
        ("down", "j"),
        "scroll",
        hint_priority=80,
        description="Scroll down a line",
        paired="transcript.up",
    ),
    Action(
        "transcript.page_up", "transcript", ("pageup",), "page up", description="Scroll up a screen"
    ),
    Action(
        "transcript.page_down",
        "transcript",
        ("pagedown",),
        "page down",
        description="Scroll down a screen",
    ),
    Action(
        "transcript.previous",
        "transcript",
        ("left",),
        "previous",
        description="Show the previous worker of the same pool",
    ),
    Action(
        "transcript.next",
        "transcript",
        ("right",),
        "siblings",
        hint_priority=40,
        description="Show the next worker of the same pool",
        paired="transcript.previous",
    ),
    Action(
        "transcript.follow",
        "transcript",
        ("f",),
        "follow",
        hint_priority=35,
        description="Follow new events",
    ),
    Action(
        "transcript.stop",
        "transcript",
        ("x",),
        "stop",
        hint_priority=60,
        description="Stop this worker; the others go on",
        when=_RUNNING_WORKER,
    ),
    Action(
        "transcript.back",
        "transcript",
        ("escape",),
        "back",
        hint_priority=1,
        description="Return to the conversation",
    ),
)

DEFAULT_ACTIONS: tuple[Action, ...] = (
    *GLOBAL_ACTIONS,
    *COMPOSER_ACTIONS,
    *AGENTS_ACTIONS,
    *AGENTS_LIST_ACTIONS,
    *AGENTS_PEEK_ACTIONS,
    *AGENTS_DETAIL_ACTIONS,
    *SUBAGENTS_ACTIONS,
    *TRANSCRIPT_ACTIONS,
)
