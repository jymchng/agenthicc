"""Agenthicc three-tier memory architecture (PRD-05).

Public API:

- :class:`MemoryTier` — session / project / global tier enum.
- :class:`SessionMemoryLayer` — in-process LRU cache with per-entry TTL.
- :class:`ProjectMemoryLayer` — SQLite-backed namespaced KV + artifacts.
- :class:`GlobalMemoryLayer` — user-wide SQLite store.
- :class:`MemoryRouter` — single dispatch point with permission checks.
- :class:`SemanticIndex` — TF-IDF / bag-of-words similarity search.
"""

from .layers import (
    ArtifactRecord,
    GlobalMemoryLayer,
    MemoryTier,
    ProjectMemoryLayer,
    SessionEntry,
    SessionMemoryLayer,
)
from .router import MemoryRouter, PermissionChecker, allow_all
from .vector import SemanticIndex

__all__ = [
    "ArtifactRecord",
    "GlobalMemoryLayer",
    "MemoryRouter",
    "MemoryTier",
    "PermissionChecker",
    "ProjectMemoryLayer",
    "SemanticIndex",
    "SessionEntry",
    "SessionMemoryLayer",
    "allow_all",
]
