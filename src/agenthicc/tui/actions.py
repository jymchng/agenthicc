"""The default key actions of every screen (AGX-13).

Each screen's dispatch, footer hints and help come from these entries through
:class:`agenthicc.tui.keymap.Keymap`. Conditions are the flags a screen reports
about its state; the composer reports:

- ``running`` — a turn is in progress (input is queued);
- ``paste`` — a large paste is shown condensed;
- ``paste_at_cursor`` — the cursor is right after that condensed paste;
- ``empty`` — the prompt is empty;
- ``queued`` — messages are queued for after the running turn (TUX-04).

The ``picker``, ``search`` and ``queue`` contexts are the composer's ``/``/``@``
pickers, its Ctrl+R history search and its queued-message rows (TUX-04).

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
(AGX-08), and ``subagents_running`` while a pool still has live workers. Ctrl+B
backgrounds that active pool. The panel and a worker's transcript report ``worker`` (the selected
worker has a transcript), ``running_worker`` (it has not finished) and
``failed_worker`` (it failed and its row has not been dismissed).
"""

from __future__ import annotations

from dataclasses import replace

from agenthicc.tui.keymap import Action

__all__ = [
    "AGENTS_ACTIONS",
    "AGENTS_DETAIL_ACTIONS",
    "AGENTS_LIST_ACTIONS",
    "AGENTS_PEEK_ACTIONS",
    "COMPOSER_ACTIONS",
    "DEFAULT_ACTIONS",
    "GLOBAL_ACTIONS",
    "PICKER_ACTIONS",
    "QUEUE_ACTIONS",
    "SEARCH_ACTIONS",
    "SUBAGENTS_ACTIONS",
    "TRANSCRIPT_ACTIONS",
]

_RUNNING = frozenset({"running"})
_PASTE = frozenset({"paste"})
_PASTE_AT_CURSOR = frozenset({"paste_at_cursor"})
_EMPTY = frozenset({"empty"})

GLOBAL_ACTIONS: tuple[Action, ...] = ()

_COMPOSER_ACTIONS: tuple[Action, ...] = (
    Action(
        "composer.submit",
        "composer",
        ("enter",),
        "submit",
        hint_priority=90,
        description="Send the prompt; while a turn runs it is queued for after the turn",
    ),
    Action(
        "composer.newline",
        "composer",
        # The footer shows the first key: Ctrl+J works in every terminal,
        # Shift+Enter only where the terminal reports it (TUX-04 §2.1).
        ("ctrl+j", "shift+enter"),
        "newline",
        hint_priority=80,
        description="Insert a line break",
    ),
    Action(
        "composer.newline_alt",
        "composer",
        ("alt+enter", "ctrl+enter"),
        "newline",
        unless=_RUNNING,
        description="Insert a line break (while a turn runs these keys interject)",
    ),
    Action(
        "composer.interject",
        "composer",
        # Alt+Enter first: most terminals send Ctrl+Enter as a plain Enter.
        ("alt+enter", "ctrl+enter"),
        "interject",
        # Below Esc pause (70), which must stay in an 80-column footer.
        hint_priority=65,
        when=_RUNNING,
        description="Send the prompt into the running turn at its next tool boundary",
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
        "exit",
        when=_EMPTY,
        unless=_RUNNING,
        description="Exit on an empty prompt",
    ),
    Action(
        "composer.delete_back",
        "composer",
        ("backspace",),
        "delete",
        description="Delete the character (or condensed paste) before the cursor",
    ),
    Action(
        "composer.delete_forward",
        "composer",
        ("delete", "ctrl+d"),
        "delete forward",
        unless=_EMPTY,
        description="Delete the character under the cursor",
    ),
    Action(
        "composer.clear",
        "composer",
        ("ctrl+u",),
        "kill to start",
        description="Delete to the line start (into the kill ring); clear all when already there",
    ),
    Action(
        "composer.kill_to_end",
        "composer",
        ("ctrl+k",),
        "kill to end",
        description="Delete to the line end (into the kill ring)",
    ),
    Action(
        "composer.delete_word_back",
        "composer",
        ("ctrl+w", "alt+backspace"),
        "delete word",
        description="Delete the word before the cursor (into the kill ring)",
    ),
    Action(
        "composer.delete_word_forward",
        "composer",
        ("alt+d",),
        "delete next word",
        description="Delete the word after the cursor (into the kill ring)",
    ),
    Action(
        "composer.yank",
        "composer",
        ("ctrl+y",),
        "yank",
        description="Insert the last deleted (killed) text",
    ),
    Action("composer.left", "composer", ("left",), "left", description="Move the cursor left"),
    Action("composer.right", "composer", ("right",), "right", description="Move the cursor right"),
    Action(
        "composer.word_left",
        "composer",
        ("alt+b", "ctrl+left"),
        "word left",
        description="Move to the previous word",
    ),
    Action(
        "composer.word_right",
        "composer",
        ("alt+f", "ctrl+right"),
        "word right",
        description="Move to the next word",
    ),
    Action(
        "composer.home",
        "composer",
        ("home", "ctrl+a"),
        "home",
        description="Move to the line start; pressed again, to the start of the prompt",
    ),
    Action(
        "composer.end",
        "composer",
        ("end", "ctrl+e"),
        "end",
        description="Move to the line end; pressed again, to the end of the prompt",
    ),
    Action(
        "composer.undo",
        "composer",
        ("ctrl+z",),
        "undo",
        description="Undo the last edit (typing in one run is one edit)",
    ),
    Action(
        "composer.redo",
        "composer",
        ("ctrl+shift+z", "alt+z"),
        "redo",
        description="Redo the last undone edit",
    ),
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
        "composer.history_search",
        "composer",
        ("ctrl+r",),
        "search history",
        description="Search earlier prompts (Ctrl+R again for an older match)",
    ),
    Action(
        "composer.external_editor",
        "composer",
        ("ctrl+x",),
        "editor",
        description="Edit the prompt in $VISUAL or $EDITOR (Ctrl+X Ctrl+E, or Ctrl+X alone)",
    ),
    Action(
        "composer.redraw",
        "composer",
        ("ctrl+l",),
        "redraw",
        description="Redraw the live region",
    ),
    Action(
        "composer.queue_focus",
        "composer",
        ("up",),
        "edit queue",
        when=frozenset({"running", "empty", "queued"}),
        description="Move into the queued messages to edit or remove one",
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
        "agents.cycle",
        "composer",
        ("tab",),
        "agent",
        description="Talk to the next primary agent (from the next turn)",
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
    Action(
        "subagents.focus",
        "composer",
        ("ctrl+o",),
        "subagents",
        hint_priority=45,
        description="Move to the subagent panel",
        when=frozenset({"subagents"}),
    ),
)

#: Actions (or keys) added by TUX-04: a user binding of one of their keys,
#: written before they existed, keeps its key (the checker reports it).
_YIELDING = frozenset(
    {
        "composer.newline",
        "composer.newline_alt",
        "composer.interject",
        "composer.delete_forward",
        "composer.kill_to_end",
        "composer.delete_word_back",
        "composer.delete_word_forward",
        "composer.yank",
        "composer.word_left",
        "composer.word_right",
        "composer.home",
        "composer.end",
        "composer.undo",
        "composer.redo",
        "composer.history_search",
        "composer.external_editor",
        "composer.redraw",
        "composer.queue_focus",
    }
)
COMPOSER_ACTIONS: tuple[Action, ...] = tuple(
    replace(action, yields=True) if action.name in _YIELDING else action
    for action in _COMPOSER_ACTIONS
)

#: The ``/``, ``$`` and ``@`` pickers (TUX-04 §2.2): only navigation and
#: choice; every other key edits the prompt and the picker follows it.
PICKER_ACTIONS: tuple[Action, ...] = (
    Action("picker.up", "picker", ("up", "ctrl+p"), "up", description="Select the previous match"),
    Action(
        "picker.down",
        "picker",
        ("down", "ctrl+n"),
        "select",
        hint_priority=90,
        description="Select the next match",
        paired="picker.up",
    ),
    Action("picker.page_up", "picker", ("pageup",), "page up", description="Move up a page"),
    Action(
        "picker.page_down", "picker", ("pagedown",), "page down", description="Move down a page"
    ),
    Action(
        "picker.complete",
        "picker",
        ("tab",),
        "complete",
        hint_priority=80,
        description="Complete the selected match without running it",
    ),
    Action(
        "picker.choose",
        "picker",
        ("enter",),
        "choose",
        hint_priority=85,
        description=(
            "Run a command that needs no argument, or complete it and list its arguments; "
            "insert a mention"
        ),
    ),
    Action(
        "picker.close",
        "picker",
        ("escape",),
        "close",
        hint_priority=10,
        description="Close the picker and keep the text",
    ),
)

#: Ctrl+R reverse history search (TUX-04 §2.3).
SEARCH_ACTIONS: tuple[Action, ...] = (
    Action(
        "search.older",
        "search",
        ("ctrl+r",),
        "older",
        hint_priority=90,
        description="Show the next older match",
    ),
    Action(
        "search.newer",
        "search",
        ("ctrl+s",),
        "newer",
        description="Show the next newer match",
    ),
    Action(
        "search.accept",
        "search",
        ("enter", "right"),
        "use",
        hint_priority=80,
        description="Put the match into the prompt to edit (it is not sent)",
    ),
    Action(
        "search.cancel",
        "search",
        ("escape", "ctrl+g"),
        "cancel",
        hint_priority=70,
        description="Close the search and restore the draft",
    ),
)

#: The queued messages above the prompt, after ↑ on an empty prompt (TUX-04 §2.6).
QUEUE_ACTIONS: tuple[Action, ...] = (
    Action("queue.up", "queue", ("up",), "up", description="Select the previous queued message"),
    Action(
        "queue.down",
        "queue",
        ("down",),
        "select",
        hint_priority=90,
        description="Select the next queued message; past the last, return to the prompt",
        paired="queue.up",
    ),
    Action(
        "queue.edit",
        "queue",
        ("enter",),
        "edit",
        hint_priority=85,
        description="Move the message back into the prompt to edit it",
    ),
    Action(
        "queue.remove",
        "queue",
        ("delete", "backspace"),
        "remove",
        hint_priority=80,
        description="Remove the message from the queue",
    ),
    Action(
        "queue.back",
        "queue",
        ("escape",),
        "back",
        hint_priority=70,
        description="Return to the prompt",
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
    unless: frozenset[str] = frozenset(),
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
        unless=unless,
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
        "Retry as a new agent, with the same request and configuration",
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
    _action(
        "agents.list",
        "library_create",
        ("n",),
        "new",
        "Create a Markdown agent definition",
        when=_flag("library_tab"),
        unless=_flag("needs_approval"),
    ),
    _action(
        "agents.list",
        "library_generate",
        ("g",),
        "generate",
        "Generate a definition draft with the current model",
        when=_flag("library_tab"),
        unless=_flag("running_tab"),
    ),
    _action(
        "agents.list",
        "library_edit",
        ("e",),
        "edit",
        "Edit the selected Markdown definition",
        when=_flag("editable_definition"),
        unless=_flag("session"),
    ),
    _action(
        "agents.list",
        "library_duplicate",
        ("d",),
        "duplicate",
        "Duplicate the selected definition",
        when=_flag("definition"),
    ),
    _action(
        "agents.list",
        "library_delete",
        ("ctrl+x",),
        "delete",
        "Delete the selected user or project definition",
        when=_flag("editable_definition"),
        unless=_flag("session"),
    ),
    _action(
        "agents.list",
        "library_default",
        ("*",),
        "set default",
        "Set the selected primary agent as the project default",
        when=_flag("primary_definition"),
    ),
    _action(
        "agents.list",
        "library_validate",
        ("v",),
        "validate",
        "Validate all agent definitions",
        when=_flag("library_tab"),
        unless=_flag("session"),
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
        "subagents.retry",
        "subagents",
        ("r",),
        "retry",
        hint_priority=58,
        description="Retry this failed worker without rerunning its siblings",
        when=frozenset({"retryable_worker"}),
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
    *PICKER_ACTIONS,
    *SEARCH_ACTIONS,
    *QUEUE_ACTIONS,
    *AGENTS_ACTIONS,
    *AGENTS_LIST_ACTIONS,
    *AGENTS_PEEK_ACTIONS,
    *AGENTS_DETAIL_ACTIONS,
    *SUBAGENTS_ACTIONS,
    *TRANSCRIPT_ACTIONS,
)
