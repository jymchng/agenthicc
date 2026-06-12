"""MemoryRouter — single dispatch point for all memory operations (PRD-05).

All agent memory access is mediated through this router: tier resolution,
permission checks, and artifact publication.  Agents never touch the backing
layers directly.
"""

from __future__ import annotations

from typing import Any, Callable

from .layers import (
    GlobalMemoryLayer,
    MemoryTier,
    ProjectMemoryLayer,
    SessionMemoryLayer,
)

__all__ = ["MemoryRouter", "PermissionChecker", "allow_all"]

# (agent_id, tier, operation) -> bool.  operation is "read" or "write".
PermissionChecker = Callable[[str | None, MemoryTier, str], bool]


def allow_all(
    agent_id: str | None, tier: MemoryTier, operation: str
) -> bool:  # noqa: ARG001
    """Default permission checker: every agent may do everything."""
    return True


_PERMISSION_DENIED = "permission_denied"


class MemoryRouter:
    """Routes memory reads/writes to the correct tier with permission checks.

    :param session_layer: Tier-1 in-process LRU layer.
    :param project_layer: Tier-2 SQLite project layer.
    :param global_layer: Tier-3 SQLite user-wide layer.
    :param permission_checker: ``callable(agent_id, tier, operation) -> bool``.
        Defaults to allow-all.  Denied operations return
        ``{"ok"/"found": False, "error": "permission_denied"}`` rather than
        raising, so the result can be surfaced directly as a tool payload.
    """

    def __init__(
        self,
        session_layer: SessionMemoryLayer,
        project_layer: ProjectMemoryLayer,
        global_layer: GlobalMemoryLayer,
        permission_checker: PermissionChecker | None = None,
    ) -> None:
        self._session = session_layer
        self._project = project_layer
        self._global = global_layer
        self._check = permission_checker or allow_all

    # -- helpers -------------------------------------------------------------

    def _kv_layer(self, tier: MemoryTier) -> ProjectMemoryLayer | None:
        """Return the async SQLite-backed layer for a tier (None for session)."""
        if tier is MemoryTier.PROJECT:
            return self._project
        if tier is MemoryTier.GLOBAL_:
            return self._global
        return None

    # -- key-value API ---------------------------------------------------------

    async def read(
        self,
        key: str,
        tier: str | MemoryTier = "session",
        namespace: str = "default",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Read ``key`` from ``tier``.  Returns ``{"found": bool, "value": ...}``."""
        resolved = MemoryTier(tier)
        if not self._check(agent_id, resolved, "read"):
            return {"found": False, "value": None, "error": _PERMISSION_DENIED}

        if resolved is MemoryTier.SESSION:
            found, value = self._session.get(key, namespace=namespace)
        else:
            layer = self._kv_layer(resolved)
            assert layer is not None
            found, value = await layer.get(key, namespace=namespace)
        return {"found": found, "value": value}

    async def write(
        self,
        key: str,
        value: Any,
        tier: str | MemoryTier = "session",
        namespace: str = "default",
        ttl: float | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Write ``key=value`` to ``tier``.  Returns ``{"ok": bool, "key": key}``.

        ``ttl`` (seconds) applies to the session tier only; it is silently
        ignored on the persistent tiers, per PRD-05 §4.2.
        """
        resolved = MemoryTier(tier)
        if not self._check(agent_id, resolved, "write"):
            return {"ok": False, "key": key, "error": _PERMISSION_DENIED}

        if resolved is MemoryTier.SESSION:
            await self._session.set(key, value, namespace=namespace, ttl=ttl)
        else:
            layer = self._kv_layer(resolved)
            assert layer is not None
            await layer.set(key, value, namespace=namespace)
        return {"ok": True, "key": key}

    # -- artifact API -----------------------------------------------------------

    async def publish_artifact(
        self,
        content: str | bytes,
        content_type: str = "text/plain",
        published_by: str | None = None,
    ) -> dict[str, Any]:
        """Store a content-addressed artifact in the project layer.

        Returns ``{"artifact_id": <sha256 hex>, "size_bytes": int}``.  The
        operation is idempotent: re-publishing identical content yields the
        same ``artifact_id``.
        """
        if not self._check(published_by, MemoryTier.PROJECT, "write"):
            return {"ok": False, "artifact_id": None, "error": _PERMISSION_DENIED}

        record = await self._project.put_artifact(
            content, content_type=content_type, published_by=published_by
        )
        return {
            "ok": True,
            "artifact_id": record.artifact_id,
            "size_bytes": record.size_bytes,
        }

    async def read_artifact(
        self,
        artifact_id: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch an artifact by id.

        Returns ``{"found": bool, "content": bytes | None, "content_type": ...}``.
        """
        if not self._check(agent_id, MemoryTier.PROJECT, "read"):
            return {
                "found": False,
                "content": None,
                "content_type": None,
                "error": _PERMISSION_DENIED,
            }

        record = await self._project.get_artifact(artifact_id)
        if record is None:
            return {"found": False, "content": None, "content_type": None}
        return {
            "found": True,
            "content": record.content,
            "content_type": record.content_type,
        }
