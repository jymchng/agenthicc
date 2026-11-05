"""The tools bound to one session, for every turn it runs (AGI-03).

The foreground TUI turn, its effective-tools preview and the background
worker's direct turn all offer exactly these: ``ask_user`` (answered through
the session's approval service, an overlay in the TUI or the operator inbox
in the background), the memory tools, and the session's browser tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.tools.approval import ApprovalBroker
    from agenthicc.tools.base import ToolLike

__all__ = ["SessionToolSource", "session_tools"]


class SessionToolSource(Protocol):
    """What a session supplies to its own tools (read-only, so narrower types fit)."""

    @property
    def approval_svc(self) -> ApprovalBroker | None: ...

    @property
    def memory_router(self) -> MemoryRouter | None: ...

    @property
    def semantic_index(self) -> SemanticIndex | None: ...

    @property
    def browser_tools(self) -> Iterable[ToolLike]: ...


def session_tools(session: SessionToolSource) -> list[ToolLike]:
    """The session-bound tools for *session*: ask_user, memory and browser."""
    from agenthicc.workflows.code_plan.phase_tools import make_questions_tool
    from agenthicc.workflows.memory_tools import make_memory_tools

    return [
        *make_questions_tool(session.approval_svc),
        *make_memory_tools(session.memory_router, session.semantic_index),
        *session.browser_tools,
    ]
