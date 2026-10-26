"""Agenthicc three-tier memory architecture (PRD-05).

Public API:

- :class:`MemoryTier` — session / project / global tier enum.
- :class:`SessionMemoryLayer` — in-process LRU cache with per-entry TTL.
- :class:`ProjectMemoryLayer` — SQLite-backed namespaced KV + artifacts.
- :class:`GlobalMemoryLayer` — user-wide SQLite store.
- :class:`MemoryRouter` — single dispatch point with permission checks.
- :class:`SemanticIndex` — TF-IDF / bag-of-words similarity search.

Public names resolve lazily (AGX-02): opening the conversation journal or the
memory layers does not import the semantic index, whose lauren-ai vector store
would otherwise load the whole agent runtime before the first TUI frame.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

_EXPORTS: dict[str, str] = {
    "ArtifactRecord": "layers",
    "GlobalMemoryLayer": "layers",
    "MemoryTier": "layers",
    "ProjectMemoryLayer": "layers",
    "SessionEntry": "layers",
    "SessionMemoryLayer": "layers",
    "MemoryRouter": "router",
    "PermissionChecker": "router",
    "allow_all": "router",
    "SemanticIndex": "vector",
}
_SUBMODULES = frozenset(
    {"compactor", "journal", "journaled", "layers", "router", "tool_history", "vector"}
)


def __getattr__(name: str) -> object:
    if name in _SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value: object = vars(importlib.import_module(f".{module}", __name__))[name]
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__, *_SUBMODULES})
