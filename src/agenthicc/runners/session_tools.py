"""The tools bound to one session, for every turn it runs (AGI-03).

The foreground TUI turn, its effective-tools preview and the background
worker's direct turn all offer exactly these: ``ask_user`` (answered through
the session's approval service, an overlay in the TUI or the operator inbox
in the background), the memory tools, the session's browser tools and, unless
``[sessions] agent_messaging = false``, ``list_sessions`` and ``message_session``
(COMM-05).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agenthicc.background.session_messages import SessionMessenger
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.runners.reload import ReloadService
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

    @property
    def reload(self) -> ReloadService: ...

    @property
    def session_messenger(self) -> SessionMessenger | None: ...


def _session_message_tools(messenger: SessionMessenger) -> list[ToolLike]:
    """``list_sessions`` and ``message_session`` over *messenger* (COMM-05)."""
    from lauren_ai._tools import tool

    from agenthicc.tools.capabilities import tool_execute, tool_read

    @tool_read
    @tool()
    async def list_sessions() -> dict[str, object]:
        """List the other running agenthicc sessions of this project that you can message.

        Each row has the session id, its title, its status, its workflow and its
        latest activity. Only a session that is running now can take a message.
        """
        return {"sessions": messenger.addressable()}

    @tool_execute
    @tool()
    async def message_session(
        session_id: str, message: str, reply_to: str = ""
    ) -> dict[str, object]:
        """Send text to another running agenthicc session of this project.

        The other session reads it as untrusted context from you, named by your
        session id: it is not the user and cannot change that session's instructions,
        approvals, mode or permissions. It never starts a finished session. You do not
        get an answer back in this call; the other session may answer in its own
        turn. A turn that started from a session's message cannot message a session
        already in that exchange, so a reply to the sender is refused. ``reply_to``
        is the id of the message you answer, when you answer one.
        """
        return messenger.send(session_id, message, reply_to=reply_to)

    return [list_sessions, message_session]


def session_tools(session: SessionToolSource) -> list[ToolLike]:
    """The session-bound tools for *session*: ask_user, memory, browser and messaging."""
    from lauren_ai._tools import tool

    from agenthicc.tools.capabilities import tool_control
    from agenthicc.workflows.code_plan.phase_tools import make_questions_tool
    from agenthicc.workflows.memory_tools import make_memory_tools

    @tool_control
    @tool()
    async def reload_tools() -> dict[str, object]:
        """Refresh this session's tool registry from already trusted sources."""
        report = await session.reload.reload(frozenset({"tools"}), reason="command")
        return report.to_dict()

    messenger = session.session_messenger
    return [
        reload_tools,
        *make_questions_tool(session.approval_svc),
        *make_memory_tools(session.memory_router, session.semantic_index),
        *session.browser_tools,
        *(_session_message_tools(messenger) if messenger is not None else ()),
    ]
