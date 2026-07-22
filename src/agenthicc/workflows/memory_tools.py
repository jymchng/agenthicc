"""Agent-callable memory tools (PRD-101).

Exposes the three-tier MemoryRouter and SemanticIndex as @tool()-decorated
callables that agents can invoke during any turn.

    tools = make_memory_tools(memory_router, semantic_index)

All tools return plain dicts so their results can be surfaced directly as
tool payloads without any post-processing by the caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex


def make_memory_tools(
    memory_router: MemoryRouter | None,
    semantic_index: SemanticIndex | None,
) -> list:
    """Return agent-callable memory tools.

    When *memory_router* or *semantic_index* is ``None`` the corresponding
    tools return a ``{"ok": false, "error": "memory_not_available"}`` payload
    rather than raising, so callers never need to guard.
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415

    @_tool()
    async def memory_write(
        key: str,
        value: str,
        scope: str = "project",
        namespace: str = "default",
        ttl_seconds: float = 0.0,
    ) -> dict:
        """Persist a value in memory across agent turns or sessions.

        scope must be one of: "session" (lost on exit), "project" (persists
        in .agenthicc/memory/project.db), or "global" (persists in
        ~/.agenthicc/global.db across all projects).

        Use this to remember decisions, preferences, or context that should
        survive beyond the current conversation.  ttl_seconds only applies
        to scope="session"; it is ignored for project/global.

        Args:
            key: Identifier for this value (e.g. "approved_plan", "preferred_style").
            value: The string value to store.
            scope: Memory tier — "session", "project", or "global".
            namespace: Logical grouping within the tier (default: "default").
            ttl_seconds: Session-only expiry in seconds (0 = no expiry).
        """
        if memory_router is None:
            return {"ok": False, "error": "memory_not_available"}
        ttl = ttl_seconds if ttl_seconds > 0 else None
        return await memory_router.write(key, value, tier=scope, namespace=namespace, ttl=ttl)

    @_tool()
    async def memory_read(
        key: str,
        scope: str = "project",
        namespace: str = "default",
    ) -> dict:
        """Read a previously stored value from memory.

        Returns {"found": true, "value": ...} when the key exists, or
        {"found": false, "value": null} when it does not.

        Args:
            key: The key to look up.
            scope: Memory tier — "session", "project", or "global".
            namespace: Logical grouping within the tier (default: "default").
        """
        if memory_router is None:
            return {"found": False, "value": None, "error": "memory_not_available"}
        return await memory_router.read(key, tier=scope, namespace=namespace)

    @_tool()
    async def semantic_search(
        query: str,
        top_k: int = 5,
    ) -> dict:
        """Search past agent outputs for context similar to *query*.

        Every completed agent turn is automatically indexed.  Use this to
        recall relevant decisions, prior solutions, or past context that
        may apply to the current task.

        Returns {"results": [{"doc_id": str, "score": float}, ...]}.

        Args:
            query: Natural-language description of the context to find.
            top_k: Maximum number of results (default 5).
        """
        if semantic_index is None:
            return {"results": [], "error": "semantic_index_not_available"}
        hits = await semantic_index.search(query, top_k=top_k)
        return {"results": [{"doc_id": doc_id, "score": round(score, 4)} for doc_id, score in hits]}

    @_tool()
    async def publish_artifact(
        content: str,
        content_type: str = "text/plain",
    ) -> dict:
        """Store a content-addressed artifact in project memory.

        The operation is idempotent: publishing identical content twice
        returns the same artifact_id.  Use this to persist code snippets,
        design decisions, or generated files that should survive the session.

        Returns {"ok": true, "artifact_id": str, "size_bytes": int}.

        Args:
            content: The text content to store.
            content_type: MIME type (default "text/plain").
        """
        if memory_router is None:
            return {"ok": False, "artifact_id": None, "error": "memory_not_available"}
        return await memory_router.publish_artifact(content, content_type=content_type)

    return [memory_write, memory_read, semantic_search, publish_artifact]
