"""Plain-English phrases for key-map conditions (TUX-08 §2.4).

A key action applies under *conditions*: flags a screen reports about its
state (``library_tab``, ``needs_approval``). Help text shows them as phrases
("in Library", "except when an agent needs approval"), never as the flag
names. Every condition used by :data:`agenthicc.tui.actions.DEFAULT_ACTIONS`
has a phrase; a unit test fails on a condition without one, so the help screen
never has to.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = ["CONDITION_PHRASES", "describe_conditions", "phrase"]

#: Each phrase reads after a key's description and after "except".
CONDITION_PHRASES: Mapping[str, str] = {
    # composer
    "running": "while a turn runs",
    "paste": "when a paste is condensed",
    "paste_at_cursor": "right after a condensed paste",
    "subagents": "while the subagent panel is shown",
    "backgroundable": "while a command or pool runs in the foreground",
    "empty": "when the prompt is empty",
    "queued": "when messages are queued",
    "suggestion": "when the empty prompt suggests a command",
    "fullscreen": "in the fullscreen layout",
    "scrolled": "when the pane is scrolled up",
    # transcript view
    "turns": "when the transcript has user turns",
    "expandable": "on a tool call or an edit",
    "rewind": "when rewinding",
    "hits": "when a search has hits",
    # decision dialogs
    "approval": "on an approval request",
    "details": "when there are full details to show",
    "note": "when a note can be added",
    "questions": "when there are several questions",
    "requester": "when another agent made the request",
    # agents hub: the selected row
    "row": "on a selected row",
    "agent": "on an agent",
    "session": "on a session",
    "inbox": "on an inbox request",
    "pinned": "on a pinned agent",
    "collapsed": "on a collapsed group",
    "expanded": "on an expanded group",
    "definition": "on an agent definition",
    "editable_definition": "on your own agent definitions",
    "primary_definition": "on a primary agent definition",
    # agents hub: what the selected agent allows
    "needs_approval": "when an agent needs approval",
    "accepts_input": "when the agent is waiting for input",
    "resumable": "when the agent can resume",
    "retryable": "when the agent can be retried",
    "stoppable": "when the agent can be stopped",
    "archivable": "when the agent can be archived",
    "attachable": "when the agent can be attached",
    # agents hub: tabs and detail sections
    "running_tab": "in Running",
    "library_tab": "in Library",
    "transcript": "when there is a transcript",
    "changes": "when there are changes",
    # subagent panel and worker transcripts
    "worker": "on a worker",
    "running_worker": "on a running worker",
    "failed_worker": "on a failed worker",
    "retryable_worker": "on a worker that can be retried",
}


def phrase(condition: str) -> str:
    """The phrase for *condition*; an unknown flag reads as words, never as ``a_b``."""
    return CONDITION_PHRASES.get(condition) or "when " + condition.replace("_", " ")


def describe_conditions(when: Iterable[str], unless: Iterable[str] = ()) -> str:
    """``"in Library, except when an agent needs approval"``, or ``""`` for always."""
    parts = [phrase(flag) for flag in sorted(when)]
    parts += [f"except {phrase(flag)}" for flag in sorted(unless)]
    return ", ".join(parts)
