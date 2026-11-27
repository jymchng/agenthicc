"""Commit a tool batch's results so the runtime can add a message after them.

Lauren-ai's loop commits the results ``_execute_tools`` returns straight after it
returns.  A runner that wants the model to read something between a batch of tool
calls and its next request (a queued user message, a notice, a worker's mail)
must commit the results itself, add its message, and return no results, so the
conversation stays the valid sequence ``assistant(tool_use) -> tool_result ->
user``.  This is that commit, in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from lauren_ai import ToolCall, ToolResult

__all__ = ["commit_tool_results"]


class _ResultMemory(Protocol):
    """The part of a lauren-ai memory every kind has."""

    def add_tool_results(self, results: list[ToolResult]) -> None: ...


def commit_tool_results(
    memory: _ResultMemory, tool_calls: list[ToolCall], results: list[ToolResult]
) -> None:
    """Commit *results* to *memory* now.

    ``tool_calls`` is the approved subset.  When the memory keeps a tool-exchange
    transaction it still owns the complete assistant batch, so the results are
    committed against it and lauren-ai synthesizes stable results for the calls
    filtered out by approval.  Passing only the subset through
    ``_complete_tool_results`` would leave a missing-result batch immediately
    before the message that follows.  A plain memory has no transaction, so the
    results are completed against the subset and added.
    """
    exchange = getattr(memory, "active_tool_exchange", None)
    commit = getattr(memory, "commit_tool_exchange", None)
    if exchange is not None and callable(commit):
        commit(exchange, results)
        return
    from lauren_ai._agents._runner import _complete_tool_results

    memory.add_tool_results(_complete_tool_results(tool_calls, results))
