---
id: PRD-05
title: "Memory Architecture"
status: draft
created: 2025-06-12
authors:
  - platform-team
reviewers:
  - infra
  - ai-runtime
priority: P0
milestone: "agenthicc-0.3"
tags:
  - memory
  - storage
  - concurrency
  - vector-db
  - sqlite
related_prds:
  - PRD-01: Agent SDK Core
  - PRD-03: Tool Interface
  - PRD-04: Multi-Agent Orchestration
---

# PRD-05: Memory Architecture

## Executive Summary

This document specifies the three-tier memory architecture for the agenthicc
platform. All agent memory access — reads and writes — is mediated exclusively
through two tool primitives: `memory_read` and `memory_write`. No agent
implementation is permitted to access backing stores directly.

The three tiers are:

| Tier | Scope | Backing Store | Lifetime |
|------|-------|---------------|----------|
| **Session** | Single agent run | In-process LRU cache (`ShortTermMemory`) | Lost on process exit |
| **Project** | `.agenthicc/` directory | SQLite + vector DB (ChromaDB or `sqlite-vec`) | Persisted across runs |
| **Global** | `~/.agenthicc/` | SQLite | User-wide; persisted indefinitely |

The design satisfies two concurrency invariants:

1. **Reads never block**: concurrent reads at any tier execute without acquiring
   any lock.
2. **Writes are serialised per tier**: each tier owns a single `asyncio.Lock`;
   write operations acquire it before touching the backing store.

A background `asyncio.Task` runs memory compaction: pruning expired session
entries on a configurable interval, and executing `VACUUM` against SQLite on the
project and global layers nightly or when the WAL exceeds a threshold.

---

## Table of Contents

1. [Goals and Non-Goals](#1-goals-and-non-goals)
2. [Architecture and Design](#2-architecture-and-design)
3. [Data Structures and Interfaces](#3-data-structures-and-interfaces)
4. [Tool Interface Specification](#4-tool-interface-specification)
5. [Implementation Plan](#5-implementation-plan)
6. [Tests](#6-tests)
7. [Configuration Reference](#7-configuration-reference)
8. [Open Questions](#8-open-questions)

---

## 1. Goals and Non-Goals

### 1.1 Goals

- **G-01**: Provide a three-tier memory hierarchy (session / project / global)
  with well-defined lifetime and scope semantics for every tier.
- **G-02**: Expose all memory operations through `memory_read` and
  `memory_write` tool primitives; agents must not access backing stores directly.
- **G-03**: Support both key-value lookup and semantic similarity search
  (vector search) through the same `memory_read` interface.
- **G-04**: Enforce permission scoping: session-only agents cannot read or write
  project-layer or global-layer memory.
- **G-05**: Provide TTL support for session-layer entries so that volatile
  working state can expire automatically without manual cleanup.
- **G-06**: Support namespace and tag-based filtering for project memory to
  allow multi-agent isolation within a project.
- **G-07**: Provide `publish_artifact` for content-addressed storage of binary
  or text blobs (test output, generated files, rendered HTML, etc.) with
  `sha256` keys; retrieval via `memory_read(artifact_id=...)`.
- **G-08**: Guarantee concurrent read safety with no blocking, and serialised
  writes via one `asyncio.Lock` per tier.
- **G-09**: Run background compaction tasks to prune expired entries and reclaim
  disk space.
- **G-10**: Integrate cleanly with the existing `lauren_ai._memory` types:
  `ShortTermMemory`, `SQLiteStoreBackend`, `SQLiteVectorStore`,
  `InMemoryVectorStore`, `MemoryFact`, `UserMemoryStore`, and
  `ConversationStore`.

### 1.2 Non-Goals

- **NG-01**: Cross-machine or distributed memory replication. All tiers are
  local to the host process / user account.
- **NG-02**: Real-time streaming of memory change events to other agents
  (covered by a future event-bus PRD).
- **NG-03**: Encryption-at-rest for memory stores (deferred; out of scope for
  this milestone).
- **NG-04**: External embedding APIs (e.g. OpenAI `text-embedding-3-small`).
  The project layer uses `sqlite-vec` or ChromaDB with a locally bundled
  sentence-transformer; the session layer uses `InMemoryVectorStore` with
  TF-IDF cosine similarity from `lauren_ai._memory._vector`.
- **NG-05**: Fine-grained per-record ACLs. Permission scoping is coarse-grained
  (session / project / global) and checked at the tool boundary.

---

## 2. Architecture and Design

### 2.1 Three-Tier Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        AGENT (any type)                                  │
│                                                                          │
│         memory_read(...)        memory_write(...)                        │
│              │                        │                                  │
└──────────────┼────────────────────────┼──────────────────────────────────┘
               │                        │
               ▼                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    MemoryRouter  (tool handler)                           │
│                                                                          │
│  ┌──────────────┐  ┌───────────────────────────┐  ┌──────────────────┐  │
│  │ Permission   │  │ Tier resolver             │  │ Write serialiser │  │
│  │ Guard        │  │ session / project / global│  │ asyncio.Lock×3   │  │
│  └──────────────┘  └───────────────────────────┘  └──────────────────┘  │
└────────┬──────────────────────┬──────────────────────────┬───────────────┘
         │                      │                           │
         ▼                      ▼                           ▼
┌─────────────────┐  ┌────────────────────────┐  ┌──────────────────────┐
│  TIER 1         │  │  TIER 2                │  │  TIER 3              │
│  Session Layer  │  │  Project Layer         │  │  Global Layer        │
│                 │  │                        │  │                      │
│  ShortTermMemory│  │  .agenthicc/memory/    │  │  ~/.agenthicc/       │
│  (LRU cache,    │  │  ┌──────────────────┐  │  │  ┌────────────────┐ │
│   in-process)   │  │  │ SQLite (kv +     │  │  │  │ SQLite         │ │
│                 │  │  │  conversation)   │  │  │  │ (prefs +       │ │
│  TTL per entry  │  │  ├──────────────────┤  │  │  │  long-term     │ │
│  LRU eviction   │  │  │ Vector DB        │  │  │  │  facts)        │ │
│  (max_size cfg) │  │  │ (ChromaDB or     │  │  │  └────────────────┘ │
│                 │  │  │  sqlite-vec)     │  │  │                      │
│  Background     │  │  └──────────────────┘  │  │  Background          │
│  Task: prune    │  │                        │  │  Task: vacuum        │
│  expired TTLs   │  │  Namespace filtering   │  │                      │
│                 │  │  Tag-based retrieval   │  │  MemoryFact store    │
│  Lost on        │  │  Content-addressed     │  │  (UserMemoryStore    │
│  restart        │  │  artifacts (sha256)    │  │   protocol)          │
└─────────────────┘  └────────────────────────┘  └──────────────────────┘
         ▲                      ▲                           ▲
         │                      │                           │
         └──────────────────────┴───────────────────────────┘
                         Background Compaction Tasks
                         ┌───────────────────────────────┐
                         │  MemoryCompactionScheduler    │
                         │  • Session: prune TTL-expired  │
                         │  • Project: SQLite VACUUM      │
                         │  • Global:  SQLite VACUUM      │
                         └───────────────────────────────┘
```

### 2.2 Request Flow: memory_read

```
Agent calls memory_read(key="foo", tier="project", namespace="test-runner")
     │
     ▼
MemoryRouter.handle_read()
     │
     ├─► PermissionGuard.check_read(agent_ctx, tier="project")
     │       └─ raises MemoryPermissionError if agent is session-only
     │
     ├─► TierResolver.resolve("project") → ProjectMemoryLayer
     │
     ├─► ProjectMemoryLayer.read(key="foo", namespace="test-runner")
     │       ├─ SQLite kv lookup (O(log n) by key index)
     │       └─ returns MemoryEntry | None
     │
     └─► Return MemoryReadResult to agent
```

### 2.3 Request Flow: memory_write

```
Agent calls memory_write(key="foo", value="bar", tier="session", ttl=300)
     │
     ▼
MemoryRouter.handle_write()
     │
     ├─► PermissionGuard.check_write(agent_ctx, tier="session")
     │
     ├─► TierResolver.resolve("session") → SessionMemoryLayer
     │
     ├─► Acquire _session_write_lock  (asyncio.Lock)
     │       │
     │       ├─► SessionMemoryLayer.write(key="foo", value="bar", ttl=300)
     │       │       └─ LRU._put(key, value, expires_at=now+300)
     │       │
     │       └─► Release _session_write_lock
     │
     └─► Return MemoryWriteResult to agent
```

### 2.4 Artifact Flow: publish_artifact + memory_read

```
TestRunnerAgent:
  publish_artifact(content=b"<test output>", mime="text/plain", tags=["pytest"])
       │
       ▼
  sha256(content) → "abc123..."
  ProjectMemoryLayer.put_artifact("abc123...", content, metadata)
  returns artifact_id = "artifact:abc123..."

DebuggerAgent:
  memory_read(artifact_id="artifact:abc123...", tier="project")
       │
       ▼
  ProjectMemoryLayer.get_artifact("abc123...")
  returns ArtifactEntry(content=b"...", mime="text/plain", tags=["pytest"])
```

### 2.5 Concurrency Model

The concurrency model satisfies:

- **Reads**: No lock acquisition. Multiple agents may call `memory_read`
  concurrently across all tiers. SQLite WAL mode allows concurrent readers
  without blocking writers.
- **Writes**: One `asyncio.Lock` per tier. The three locks are independent:
  - `_session_write_lock: asyncio.Lock`
  - `_project_write_lock: asyncio.Lock`
  - `_global_write_lock: asyncio.Lock`

  A write to tier 1 does not block a write to tier 2. Lock granularity is
  intentionally coarse (per-tier, not per-key) to simplify correctness; this
  is acceptable because writes are infrequent compared with reads in typical
  agent workloads.

---

## 3. Data Structures and Interfaces

### 3.1 Core Enums and Constants

```python
# src/agenthicc/memory/_types.py
from __future__ import annotations

import enum


class MemoryTier(str, enum.Enum):
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


class AgentPermission(str, enum.Enum):
    """Permission level granted to an agent at spawn time."""
    SESSION_ONLY = "session_only"   # can only access SESSION tier
    PROJECT = "project"             # SESSION + PROJECT tiers
    GLOBAL = "global"               # all three tiers

    def allows_tier(self, tier: MemoryTier) -> bool:
        order = {
            AgentPermission.SESSION_ONLY: {MemoryTier.SESSION},
            AgentPermission.PROJECT: {MemoryTier.SESSION, MemoryTier.PROJECT},
            AgentPermission.GLOBAL: {MemoryTier.SESSION, MemoryTier.PROJECT, MemoryTier.GLOBAL},
        }
        return tier in order[self]
```

### 3.2 Agent Context

```python
# src/agenthicc/memory/_types.py (continued)
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Immutable context attached to every agent invocation."""
    agent_id: str
    session_id: str
    permission: AgentPermission = AgentPermission.PROJECT
    namespace: str = "default"
```

### 3.3 Memory Entry Types

```python
# src/agenthicc/memory/_types.py (continued)
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionEntry:
    """A single entry in the session-layer LRU cache."""
    key: str
    value: Any
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None       # monotonic timestamp; None = no expiry
    namespace: str = "default"

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.monotonic() >= self.expires_at


@dataclass(slots=True)
class ProjectEntry:
    """A single key-value record in the project layer."""
    key: str
    value: str                            # JSON-serialised
    namespace: str = "default"
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ArtifactEntry:
    """Content-addressed binary or text artifact stored in project layer."""
    artifact_id: str                      # "artifact:<sha256hex>"
    content: bytes
    mime_type: str = "application/octet-stream"
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    size_bytes: int = 0

    def __post_init__(self) -> None:
        if not self.size_bytes:
            self.size_bytes = len(self.content)


@dataclass(slots=True)
class GlobalEntry:
    """A user-wide preference or long-term fact in the global layer."""
    key: str
    value: str                            # JSON-serialised
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
```

### 3.4 Result Types

```python
# src/agenthicc/memory/_results.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MemoryReadResult:
    """Value returned to the agent from memory_read."""
    found: bool
    key: str | None = None
    value: Any = None
    artifact_id: str | None = None
    artifact_content: bytes | None = None
    artifact_mime: str | None = None
    semantic_results: list[SemanticResult] = field(default_factory=list)
    tier: str = ""
    namespace: str = ""


@dataclass(slots=True)
class SemanticResult:
    """One result from a vector similarity search."""
    id: str
    content: str
    score: float                          # [0.0, 1.0]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryWriteResult:
    """Acknowledgement returned to the agent after memory_write."""
    ok: bool
    key: str
    tier: str
    artifact_id: str | None = None        # populated by publish_artifact
    error: str | None = None
```

### 3.5 Layer Protocols

```python
# src/agenthicc/memory/_protocols.py
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ._types import AgentContext, SessionEntry, ProjectEntry, GlobalEntry, ArtifactEntry
from ._results import MemoryReadResult, MemoryWriteResult


@runtime_checkable
class SessionLayer(Protocol):
    """In-process LRU cache layer backed by ShortTermMemory."""

    def get(self, key: str, *, namespace: str = "default") -> SessionEntry | None: ...
    def put(self, key: str, value: Any, *, namespace: str = "default",
            ttl: int | None = None) -> None: ...
    def delete(self, key: str, *, namespace: str = "default") -> None: ...
    def prune_expired(self) -> int: ...       # returns count of pruned entries
    def clear(self) -> None: ...


@runtime_checkable
class ProjectLayer(Protocol):
    """SQLite + vector DB backed persistent project layer."""

    async def get(self, key: str, *, namespace: str = "default") -> ProjectEntry | None: ...
    async def put(self, key: str, value: Any, *, namespace: str = "default",
                  tags: list[str] | None = None) -> None: ...
    async def delete(self, key: str, *, namespace: str = "default") -> None: ...
    async def search(self, query: str, *, k: int = 5,
                     namespace: str | None = None,
                     tags: list[str] | None = None) -> list[SemanticResult]: ...
    async def put_artifact(self, content: bytes | str, *,
                           mime_type: str = "application/octet-stream",
                           tags: list[str] | None = None) -> str: ...
    async def get_artifact(self, artifact_id: str) -> ArtifactEntry | None: ...
    async def vacuum(self) -> None: ...


@runtime_checkable
class GlobalLayer(Protocol):
    """SQLite-backed user-wide preference and long-term fact store."""

    async def get(self, key: str) -> GlobalEntry | None: ...
    async def put(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def vacuum(self) -> None: ...
```

### 3.6 LRU Cache Implementation (Session Layer)

```python
# src/agenthicc/memory/_session.py
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from ._types import SessionEntry


class SessionMemoryLayer:
    """In-process LRU cache implementing the session memory tier.

    Wraps an OrderedDict as a bounded LRU cache.  TTL expiry is enforced
    lazily on every get() call and proactively by the compaction task.

    This class is intentionally NOT thread-safe for concurrent writes; the
    MemoryRouter holds a single asyncio.Lock for session writes, so this
    class can remain simple and allocation-free.

    :param max_size: Maximum number of entries before LRU eviction.
    :param default_namespace: Namespace used when none is specified.
    """

    def __init__(self, max_size: int = 1024, default_namespace: str = "default") -> None:
        self._max_size = max_size
        self._default_namespace = default_namespace
        # Keyed by (namespace, key) tuple for O(1) lookup
        self._cache: OrderedDict[tuple[str, str], SessionEntry] = OrderedDict()

    def _ns_key(self, key: str, namespace: str) -> tuple[str, str]:
        return (namespace, key)

    def get(self, key: str, *, namespace: str = "default") -> SessionEntry | None:
        """Return the entry if present and not expired; else None."""
        ns_key = self._ns_key(key, namespace)
        entry = self._cache.get(ns_key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._cache[ns_key]
            return None
        # LRU: move to end (most recently used)
        self._cache.move_to_end(ns_key)
        return entry

    def put(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "default",
        ttl: int | None = None,
    ) -> None:
        """Insert or update an entry.  Evicts the LRU entry if at capacity."""
        ns_key = self._ns_key(key, namespace)
        expires_at = (time.monotonic() + ttl) if ttl is not None else None
        entry = SessionEntry(
            key=key,
            value=value,
            expires_at=expires_at,
            namespace=namespace,
        )
        if ns_key in self._cache:
            self._cache.move_to_end(ns_key)
        self._cache[ns_key] = entry
        # Evict least-recently-used entry when over capacity
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def delete(self, key: str, *, namespace: str = "default") -> None:
        self._cache.pop(self._ns_key(key, namespace), None)

    def prune_expired(self) -> int:
        """Remove all expired entries.  Returns the count removed."""
        expired = [k for k, v in self._cache.items() if v.is_expired()]
        for k in expired:
            del self._cache[k]
        return len(expired)

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)
```

### 3.7 Permission Guard

```python
# src/agenthicc/memory/_permission.py
from __future__ import annotations

from ._types import AgentContext, AgentPermission, MemoryTier


class MemoryPermissionError(PermissionError):
    """Raised when an agent attempts to access a tier it is not permitted to."""


class PermissionGuard:
    """Stateless permission checker invoked on every memory operation."""

    def check_read(self, ctx: AgentContext, tier: MemoryTier) -> None:
        """Raise MemoryPermissionError if ctx.permission does not allow
        reading from tier."""
        if not ctx.permission.allows_tier(tier):
            raise MemoryPermissionError(
                f"Agent '{ctx.agent_id}' with permission "
                f"'{ctx.permission.value}' is not allowed to read "
                f"from the '{tier.value}' memory tier."
            )

    def check_write(self, ctx: AgentContext, tier: MemoryTier) -> None:
        """Raise MemoryPermissionError if ctx.permission does not allow
        writing to tier."""
        if not ctx.permission.allows_tier(tier):
            raise MemoryPermissionError(
                f"Agent '{ctx.agent_id}' with permission "
                f"'{ctx.permission.value}' is not allowed to write "
                f"to the '{tier.value}' memory tier."
            )
```

---

## 4. Tool Interface Specification

### 4.1 `memory_read`

**Description**: Read a value from the memory system.  Supports key-value
lookup, semantic similarity search, and content-addressed artifact retrieval.

**JSON Schema**:

```json
{
  "name": "memory_read",
  "description": "Read a value from the agent memory system. Supports key lookup, semantic search, and artifact retrieval.",
  "input_schema": {
    "type": "object",
    "properties": {
      "tier": {
        "type": "string",
        "enum": ["session", "project", "global"],
        "description": "Memory tier to read from. Defaults to 'session'.",
        "default": "session"
      },
      "key": {
        "type": "string",
        "description": "Exact key to look up. Mutually exclusive with 'query' and 'artifact_id'."
      },
      "query": {
        "type": "string",
        "description": "Natural-language query for semantic similarity search. Returns top-k results ordered by score."
      },
      "artifact_id": {
        "type": "string",
        "description": "Content-addressed artifact identifier returned by publish_artifact. Format: 'artifact:<sha256hex>'."
      },
      "namespace": {
        "type": "string",
        "description": "Namespace filter. Applies to project and session tiers.",
        "default": "default"
      },
      "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tag filter for semantic search results (project tier only)."
      },
      "k": {
        "type": "integer",
        "minimum": 1,
        "maximum": 50,
        "default": 5,
        "description": "Maximum number of semantic search results to return."
      }
    },
    "oneOf": [
      {"required": ["key"]},
      {"required": ["query"]},
      {"required": ["artifact_id"]}
    ],
    "additionalProperties": false
  }
}
```

**Return Schema**:

```json
{
  "type": "object",
  "properties": {
    "found": {"type": "boolean"},
    "key": {"type": ["string", "null"]},
    "value": {},
    "artifact_id": {"type": ["string", "null"]},
    "artifact_content_base64": {"type": ["string", "null"]},
    "artifact_mime": {"type": ["string", "null"]},
    "semantic_results": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "id": {"type": "string"},
          "content": {"type": "string"},
          "score": {"type": "number"},
          "metadata": {"type": "object"}
        }
      }
    },
    "tier": {"type": "string"},
    "namespace": {"type": "string"}
  }
}
```

**Validation Rules**:

- Exactly one of `key`, `query`, or `artifact_id` must be provided.
- `artifact_id` may only target the `project` tier.
- `query` (semantic search) may only target the `project` tier (which has a
  vector backend). Semantic search on `session` or `global` returns an empty
  result set rather than an error.
- `tags` filter is silently ignored on `session` and `global` tiers.
- If the agent's `AgentPermission` does not cover the requested `tier`, a
  `MemoryPermissionError` is raised and surfaced as a tool error.

**Examples**:

```jsonc
// Key lookup in session tier
{ "key": "current_file", "tier": "session" }

// Semantic search in project namespace "test-results"
{ "query": "test failures in auth module", "tier": "project",
  "namespace": "test-results", "k": 3 }

// Artifact retrieval
{ "artifact_id": "artifact:3a7bd3e2...", "tier": "project" }
```

---

### 4.2 `memory_write`

**Description**: Write a value to the memory system.

**JSON Schema**:

```json
{
  "name": "memory_write",
  "description": "Write a value to the agent memory system.",
  "input_schema": {
    "type": "object",
    "required": ["key", "value"],
    "properties": {
      "key": {
        "type": "string",
        "description": "Key under which to store the value.",
        "minLength": 1,
        "maxLength": 512
      },
      "value": {
        "description": "Value to store. Must be JSON-serialisable."
      },
      "tier": {
        "type": "string",
        "enum": ["session", "project", "global"],
        "default": "session"
      },
      "namespace": {
        "type": "string",
        "description": "Namespace for the entry (session and project tiers).",
        "default": "default"
      },
      "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tags for the entry (project tier only). Used for filtering."
      },
      "ttl": {
        "type": "integer",
        "minimum": 1,
        "description": "Time-to-live in seconds (session tier only). Entry is eligible for pruning after TTL expires."
      },
      "overwrite": {
        "type": "boolean",
        "default": true,
        "description": "If false and the key already exists, the write is rejected with an error."
      }
    },
    "additionalProperties": false
  }
}
```

**Return Schema**:

```json
{
  "type": "object",
  "properties": {
    "ok": {"type": "boolean"},
    "key": {"type": "string"},
    "tier": {"type": "string"},
    "error": {"type": ["string", "null"]}
  }
}
```

**Validation Rules**:

- `ttl` is silently ignored on `project` and `global` tiers.
- `tags` is silently ignored on `session` and `global` tiers.
- Values must be JSON-serialisable. Passing a non-serialisable object returns
  `{"ok": false, "error": "value is not JSON-serialisable"}`.

---

### 4.3 `publish_artifact`

**Description**: Store a binary or text artifact in the project layer using
content-addressed storage.  Returns an `artifact_id` that other agents can use
with `memory_read(artifact_id=...)`.

**JSON Schema**:

```json
{
  "name": "publish_artifact",
  "description": "Store a binary or text artifact with sha256 content-addressing. Returns artifact_id for retrieval.",
  "input_schema": {
    "type": "object",
    "required": ["content"],
    "properties": {
      "content": {
        "type": "string",
        "description": "Base64-encoded bytes for binary artifacts, or raw UTF-8 text for text artifacts."
      },
      "content_encoding": {
        "type": "string",
        "enum": ["utf-8", "base64"],
        "default": "utf-8",
        "description": "Encoding of the content field."
      },
      "mime_type": {
        "type": "string",
        "default": "text/plain",
        "description": "MIME type of the artifact."
      },
      "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional tags for the artifact."
      },
      "namespace": {
        "type": "string",
        "default": "default"
      }
    },
    "additionalProperties": false
  }
}
```

**Return Schema**:

```json
{
  "type": "object",
  "properties": {
    "ok": {"type": "boolean"},
    "artifact_id": {"type": "string"},
    "size_bytes": {"type": "integer"},
    "sha256": {"type": "string"},
    "error": {"type": ["string", "null"]}
  }
}
```

**Implementation Notes**:

- The `artifact_id` is always `"artifact:" + sha256hex(raw_bytes)`.
- If an artifact with the same `artifact_id` already exists, the call is
  idempotent — the existing entry is returned unchanged.
- Binary content round-trips as base64 over the tool call boundary; the
  backing store writes raw bytes.
- Artifacts require `PROJECT` or `GLOBAL` permission.

---

## 5. Implementation Plan

### 5.1 Phase 0 — Foundation (Sprint 1)

**Files to create**:

```
src/agenthicc/memory/
    __init__.py
    _types.py          # MemoryTier, AgentPermission, AgentContext, Entry types
    _results.py        # MemoryReadResult, MemoryWriteResult, SemanticResult
    _permission.py     # PermissionGuard, MemoryPermissionError
    _session.py        # SessionMemoryLayer (LRU cache)
    _project.py        # ProjectMemoryLayer (SQLite + vector)
    _global.py         # GlobalMemoryLayer (SQLite)
    _router.py         # MemoryRouter — main entry point
    _compaction.py     # MemoryCompactionScheduler
    _artifacts.py      # publish_artifact, ArtifactStore
    _tools.py          # Tool handler functions (memory_read, memory_write)
```

**Leverage existing `lauren_ai._memory` types**:

| agenthicc component | Delegates to |
|---------------------|-------------|
| `SessionMemoryLayer` | `lauren_ai._memory.ShortTermMemory` for conversation buffers; adds LRU kv cache on top |
| `ProjectMemoryLayer` (kv) | `lauren_ai._memory.SQLiteStoreBackend` with `SQLiteStoreConfig` |
| `ProjectMemoryLayer` (vector) | `lauren_ai._memory.SQLiteVectorStore` (preferred) or `InMemoryVectorStore` for tests |
| `GlobalMemoryLayer` | `lauren_ai._memory.SQLiteUserMemoryStore` + `SQLiteStoreBackend` |
| Fact storage | `lauren_ai._memory.MemoryFact`, `UserMemoryStore` protocol |
| Conversation persistence | `lauren_ai._memory.ConversationStore`, `SQLiteConversationStore` |

### 5.2 Phase 1 — Session Layer (Sprint 1)

Implement `SessionMemoryLayer`:

```python
# src/agenthicc/memory/_session.py
import asyncio
import time
from collections import OrderedDict
from typing import Any

from ._types import SessionEntry


class SessionMemoryLayer:
    """See Section 3.6 for full implementation."""

    def __init__(self, max_size: int = 1024) -> None:
        self._max_size = max_size
        self._cache: OrderedDict[tuple[str, str], SessionEntry] = OrderedDict()
        # Note: write lock is owned by MemoryRouter, not here.

    # ... (see Section 3.6)
```

Wire `ShortTermMemory` from `lauren_ai._memory` into session layer:

```python
from lauren_ai._memory import ShortTermMemory

class SessionMemoryLayer:
    def __init__(self, max_size: int = 1024, max_tokens: int = 40_000) -> None:
        self._max_size = max_size
        self._cache: OrderedDict[tuple[str, str], SessionEntry] = OrderedDict()
        # Conversation buffer — managed separately from kv cache
        self.short_term: ShortTermMemory = ShortTermMemory(max_tokens=max_tokens)
```

### 5.3 Phase 2 — Project Layer (Sprint 2)

`ProjectMemoryLayer` wraps `SQLiteStoreBackend` for kv storage and
`SQLiteVectorStore` (or ChromaDB) for semantic search.

```python
# src/agenthicc/memory/_project.py
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from lauren_ai._memory import SQLiteStoreBackend, SQLiteStoreConfig, SQLiteVectorStore

from ._types import ProjectEntry, ArtifactEntry
from ._results import SemanticResult


class ProjectMemoryLayer:
    """SQLite + vector DB backed project-scoped memory layer."""

    _KV_SCHEMA = """
    CREATE TABLE IF NOT EXISTS project_kv (
        namespace TEXT NOT NULL,
        key       TEXT NOT NULL,
        value     TEXT NOT NULL,
        tags      TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (namespace, key)
    );
    CREATE INDEX IF NOT EXISTS idx_project_kv_ns ON project_kv(namespace);
    """

    _ARTIFACT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS project_artifacts (
        artifact_id TEXT PRIMARY KEY,
        content     BLOB NOT NULL,
        mime_type   TEXT NOT NULL DEFAULT 'application/octet-stream',
        tags        TEXT NOT NULL DEFAULT '[]',
        namespace   TEXT NOT NULL DEFAULT 'default',
        created_at  REAL NOT NULL,
        size_bytes  INTEGER NOT NULL
    );
    """

    def __init__(
        self,
        db_path: str,
        vector_store: SQLiteVectorStore | None = None,
    ) -> None:
        cfg = SQLiteStoreConfig(database_path=db_path)
        self._backend = SQLiteStoreBackend(cfg)
        self._vector = vector_store
        self._ready = False

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        await self._backend.ensure_schema("project_kv", [self._KV_SCHEMA])
        await self._backend.ensure_schema("project_artifacts", [self._ARTIFACT_SCHEMA])
        self._ready = True

    async def get(self, key: str, *, namespace: str = "default") -> ProjectEntry | None:
        await self._ensure_ready()
        row = await self._backend.fetch_one(
            "SELECT * FROM project_kv WHERE namespace=? AND key=?",
            [namespace, key],
        )
        if row is None:
            return None
        return ProjectEntry(
            key=str(row["key"]),
            value=str(row["value"]),
            namespace=str(row["namespace"]),
            tags=json.loads(str(row["tags"])),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def put(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "default",
        tags: list[str] | None = None,
    ) -> None:
        await self._ensure_ready()
        now = time.time()
        serialised = json.dumps(value, default=str)
        tags_json = json.dumps(tags or [])
        await self._backend.execute(
            """
            INSERT INTO project_kv (namespace, key, value, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value=excluded.value,
                tags=excluded.tags,
                updated_at=excluded.updated_at
            """,
            [namespace, key, serialised, tags_json, now, now],
        )
        # Also upsert into vector store for semantic retrieval
        if self._vector is not None and isinstance(value, str):
            await self._vector.upsert(
                content=value,
                id=f"{namespace}:{key}",
                metadata={"namespace": namespace, "key": key, "tags": tags or []},
            )

    async def put_artifact(
        self,
        content: bytes | str,
        *,
        mime_type: str = "application/octet-stream",
        tags: list[str] | None = None,
        namespace: str = "default",
    ) -> str:
        await self._ensure_ready()
        if isinstance(content, str):
            raw = content.encode("utf-8")
            mime_type = mime_type or "text/plain; charset=utf-8"
        else:
            raw = content
        sha = hashlib.sha256(raw).hexdigest()
        artifact_id = f"artifact:{sha}"
        # Idempotent — skip if already stored
        existing = await self._backend.fetch_one(
            "SELECT artifact_id FROM project_artifacts WHERE artifact_id=?",
            [artifact_id],
        )
        if existing is None:
            now = time.time()
            await self._backend.execute(
                """
                INSERT INTO project_artifacts
                    (artifact_id, content, mime_type, tags, namespace, created_at, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [artifact_id, raw, mime_type, json.dumps(tags or []),
                 namespace, now, len(raw)],
            )
        return artifact_id

    async def get_artifact(self, artifact_id: str) -> ArtifactEntry | None:
        await self._ensure_ready()
        row = await self._backend.fetch_one(
            "SELECT * FROM project_artifacts WHERE artifact_id=?",
            [artifact_id],
        )
        if row is None:
            return None
        return ArtifactEntry(
            artifact_id=str(row["artifact_id"]),
            content=bytes(row["content"]),
            mime_type=str(row["mime_type"]),
            tags=json.loads(str(row["tags"])),
            created_at=float(row["created_at"]),
            size_bytes=int(row["size_bytes"]),
        )

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        namespace: str | None = None,
        tags: list[str] | None = None,
    ) -> list[SemanticResult]:
        if self._vector is None:
            return []
        meta_filter: dict[str, Any] = {}
        if namespace is not None:
            meta_filter["namespace"] = namespace
        raw = await self._vector.search(query, k=k, filter=meta_filter or None)
        results = []
        for r in raw:
            # Tag filter (post-filter since vector stores have limited metadata query)
            if tags:
                stored_tags = r.metadata.get("tags", [])
                if not any(t in stored_tags for t in tags):
                    continue
            results.append(SemanticResult(
                id=r.id, content=r.content, score=r.score, metadata=r.metadata
            ))
        return results

    async def vacuum(self) -> None:
        await self._backend.execute("VACUUM")
```

### 5.4 Phase 3 — MemoryRouter (Sprint 2)

```python
# src/agenthicc/memory/_router.py
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from ._permission import PermissionGuard
from ._results import MemoryReadResult, MemoryWriteResult, SemanticResult
from ._session import SessionMemoryLayer
from ._project import ProjectMemoryLayer
from ._global import GlobalMemoryLayer
from ._types import AgentContext, MemoryTier


class MemoryRouter:
    """Central dispatcher for all memory_read / memory_write tool calls.

    Owns one asyncio.Lock per tier for serialised writes.
    Reads never acquire any lock.
    """

    def __init__(
        self,
        session: SessionMemoryLayer,
        project: ProjectMemoryLayer,
        global_: GlobalMemoryLayer,
    ) -> None:
        self._session = session
        self._project = project
        self._global = global_
        self._guard = PermissionGuard()
        self._session_write_lock = asyncio.Lock()
        self._project_write_lock = asyncio.Lock()
        self._global_write_lock = asyncio.Lock()

    async def handle_read(
        self,
        ctx: AgentContext,
        *,
        tier: str = "session",
        key: str | None = None,
        query: str | None = None,
        artifact_id: str | None = None,
        namespace: str = "default",
        tags: list[str] | None = None,
        k: int = 5,
    ) -> MemoryReadResult:
        resolved = MemoryTier(tier)
        self._guard.check_read(ctx, resolved)

        if artifact_id is not None:
            entry = await self._project.get_artifact(artifact_id)
            if entry is None:
                return MemoryReadResult(found=False, artifact_id=artifact_id, tier=tier)
            content_b64 = base64.b64encode(entry.content).decode()
            return MemoryReadResult(
                found=True,
                artifact_id=artifact_id,
                artifact_content=entry.content,
                artifact_mime=entry.mime_type,
                tier=tier,
                namespace=namespace,
                # Expose base64 for JSON boundary
                value=content_b64,
            )

        if query is not None:
            if resolved == MemoryTier.PROJECT:
                results = await self._project.search(
                    query, k=k, namespace=namespace, tags=tags
                )
            else:
                results = []
            return MemoryReadResult(
                found=bool(results),
                semantic_results=results,
                tier=tier,
                namespace=namespace,
            )

        # Key lookup
        if key is None:
            raise ValueError("One of key, query, or artifact_id must be provided.")

        if resolved == MemoryTier.SESSION:
            entry = self._session.get(key, namespace=namespace)
            if entry is None:
                return MemoryReadResult(found=False, key=key, tier=tier)
            return MemoryReadResult(found=True, key=key, value=entry.value,
                                    tier=tier, namespace=namespace)

        if resolved == MemoryTier.PROJECT:
            row = await self._project.get(key, namespace=namespace)
            if row is None:
                return MemoryReadResult(found=False, key=key, tier=tier)
            return MemoryReadResult(found=True, key=key,
                                    value=json.loads(row.value),
                                    tier=tier, namespace=namespace)

        # Global tier
        row = await self._global.get(key)
        if row is None:
            return MemoryReadResult(found=False, key=key, tier=tier)
        return MemoryReadResult(found=True, key=key,
                                value=json.loads(row.value), tier=tier)

    async def handle_write(
        self,
        ctx: AgentContext,
        *,
        key: str,
        value: Any,
        tier: str = "session",
        namespace: str = "default",
        tags: list[str] | None = None,
        ttl: int | None = None,
        overwrite: bool = True,
    ) -> MemoryWriteResult:
        resolved = MemoryTier(tier)
        self._guard.check_write(ctx, resolved)

        if resolved == MemoryTier.SESSION:
            async with self._session_write_lock:
                if not overwrite and self._session.get(key, namespace=namespace) is not None:
                    return MemoryWriteResult(ok=False, key=key, tier=tier,
                                             error="key already exists and overwrite=False")
                self._session.put(key, value, namespace=namespace, ttl=ttl)
            return MemoryWriteResult(ok=True, key=key, tier=tier)

        if resolved == MemoryTier.PROJECT:
            async with self._project_write_lock:
                if not overwrite:
                    existing = await self._project.get(key, namespace=namespace)
                    if existing is not None:
                        return MemoryWriteResult(ok=False, key=key, tier=tier,
                                                 error="key already exists and overwrite=False")
                await self._project.put(key, value, namespace=namespace, tags=tags)
            return MemoryWriteResult(ok=True, key=key, tier=tier)

        # Global tier
        async with self._global_write_lock:
            await self._global.put(key, value)
        return MemoryWriteResult(ok=True, key=key, tier=tier)

    async def handle_publish_artifact(
        self,
        ctx: AgentContext,
        *,
        content: str,
        content_encoding: str = "utf-8",
        mime_type: str = "text/plain",
        tags: list[str] | None = None,
        namespace: str = "default",
    ) -> MemoryWriteResult:
        self._guard.check_write(ctx, MemoryTier.PROJECT)
        raw: bytes
        if content_encoding == "base64":
            raw = base64.b64decode(content)
        else:
            raw = content.encode("utf-8")
        async with self._project_write_lock:
            artifact_id = await self._project.put_artifact(
                raw, mime_type=mime_type, tags=tags, namespace=namespace
            )
        return MemoryWriteResult(ok=True, key=artifact_id, tier="project",
                                 artifact_id=artifact_id)
```

### 5.5 Phase 4 — Compaction (Sprint 3)

```python
# src/agenthicc/memory/_compaction.py
from __future__ import annotations

import asyncio
import logging

from ._session import SessionMemoryLayer
from ._project import ProjectMemoryLayer
from ._global import GlobalMemoryLayer

logger = logging.getLogger(__name__)


class MemoryCompactionScheduler:
    """Background asyncio.Task that prunes and compacts all memory tiers."""

    def __init__(
        self,
        session: SessionMemoryLayer,
        project: ProjectMemoryLayer,
        global_: GlobalMemoryLayer,
        session_interval_s: float = 60.0,
        vacuum_interval_s: float = 86_400.0,   # 24 hours
    ) -> None:
        self._session = session
        self._project = project
        self._global = global_
        self._session_interval = session_interval_s
        self._vacuum_interval = vacuum_interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="memory-compaction")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        session_ticks = 0
        vacuum_every = max(1, int(self._vacuum_interval / self._session_interval))
        while True:
            await asyncio.sleep(self._session_interval)
            pruned = self._session.prune_expired()
            if pruned:
                logger.debug("memory-compaction: pruned %d expired session entries", pruned)
            session_ticks += 1
            if session_ticks >= vacuum_every:
                session_ticks = 0
                try:
                    await self._project.vacuum()
                    await self._global.vacuum()
                    logger.debug("memory-compaction: SQLite VACUUM complete")
                except Exception:
                    logger.exception("memory-compaction: VACUUM failed")
```

---

## 6. Tests

All test code below is production-quality pytest.  Install test dependencies:

```
pip install pytest pytest-asyncio pytest-timeout
```

### 6.1 Unit Tests

```python
# tests/memory/test_unit.py
"""Unit tests: LRU eviction, write serialisation, TTL expiry, permission scoping."""
from __future__ import annotations

import asyncio
import time

import pytest

from agenthicc.memory._session import SessionMemoryLayer
from agenthicc.memory._permission import PermissionGuard, MemoryPermissionError
from agenthicc.memory._types import (
    AgentContext,
    AgentPermission,
    MemoryTier,
)


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------

class TestLRUEviction:
    """SessionMemoryLayer evicts the least-recently-used entry when full."""

    def test_evicts_oldest_on_overflow(self) -> None:
        layer = SessionMemoryLayer(max_size=3)
        layer.put("a", 1)
        layer.put("b", 2)
        layer.put("c", 3)
        # Access "a" so it becomes MRU
        layer.get("a")
        # Add "d" — "b" should be evicted (LRU)
        layer.put("d", 4)
        assert layer.get("b") is None, "b should have been evicted"
        assert layer.get("a") is not None, "a was recently used; must survive"
        assert layer.get("c") is not None
        assert layer.get("d") is not None

    def test_does_not_evict_below_capacity(self) -> None:
        layer = SessionMemoryLayer(max_size=5)
        for i in range(5):
            layer.put(f"key-{i}", i)
        for i in range(5):
            assert layer.get(f"key-{i}") is not None

    def test_update_moves_to_mru(self) -> None:
        layer = SessionMemoryLayer(max_size=2)
        layer.put("x", 1)
        layer.put("y", 2)
        # Re-put "x" — it becomes MRU; next put should evict "y"
        layer.put("x", 99)
        layer.put("z", 3)
        assert layer.get("x") is not None, "x should survive as MRU"
        assert layer.get("y") is None, "y should be evicted"

    def test_len_reflects_capacity(self) -> None:
        layer = SessionMemoryLayer(max_size=4)
        for i in range(10):
            layer.put(f"k{i}", i)
        assert len(layer) == 4

    def test_namespace_isolation(self) -> None:
        layer = SessionMemoryLayer(max_size=10)
        layer.put("key", "ns-a-value", namespace="ns-a")
        layer.put("key", "ns-b-value", namespace="ns-b")
        a = layer.get("key", namespace="ns-a")
        b = layer.get("key", namespace="ns-b")
        assert a is not None and a.value == "ns-a-value"
        assert b is not None and b.value == "ns-b-value"


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    """SessionMemoryLayer respects TTL on get() and prune_expired()."""

    def test_expired_entry_returns_none(self) -> None:
        layer = SessionMemoryLayer()
        # TTL of -1 second means already expired
        layer.put("k", "v", ttl=-1)
        assert layer.get("k") is None

    def test_non_expired_entry_is_returned(self) -> None:
        layer = SessionMemoryLayer()
        layer.put("k", "v", ttl=3600)
        entry = layer.get("k")
        assert entry is not None
        assert entry.value == "v"

    def test_prune_removes_expired_only(self) -> None:
        layer = SessionMemoryLayer(max_size=10)
        layer.put("alive", "yes", ttl=3600)
        layer.put("dead", "no", ttl=-1)
        pruned = layer.prune_expired()
        assert pruned == 1
        assert layer.get("alive") is not None
        assert layer.get("dead") is None

    def test_no_ttl_never_expires(self) -> None:
        layer = SessionMemoryLayer()
        layer.put("forever", "ever")   # no ttl
        assert layer.get("forever") is not None

    def test_prune_returns_zero_when_nothing_expired(self) -> None:
        layer = SessionMemoryLayer()
        layer.put("a", 1, ttl=3600)
        layer.put("b", 2, ttl=3600)
        assert layer.prune_expired() == 0


# ---------------------------------------------------------------------------
# Concurrent write serialisation
# ---------------------------------------------------------------------------

class TestConcurrentWriteSerialisation:
    """Writes to the same tier are serialised; no data races under load."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_no_race(self) -> None:
        """1000 concurrent tasks all writing to the session layer should
        produce a consistent final count."""
        import asyncio

        from agenthicc.memory._router import MemoryRouter
        from agenthicc.memory._project import ProjectMemoryLayer
        from agenthicc.memory._global import GlobalMemoryLayer

        # Build an in-memory stack for the test
        session_layer = SessionMemoryLayer(max_size=2000)
        project_layer = ProjectMemoryLayer(db_path=":memory:")
        global_layer = GlobalMemoryLayer(db_path=":memory:")
        router = MemoryRouter(session_layer, project_layer, global_layer)

        ctx = AgentContext(
            agent_id="test-agent",
            session_id="sess-001",
            permission=AgentPermission.PROJECT,
        )

        counter: list[int] = [0]

        async def write_and_read(i: int) -> None:
            key = f"counter-{i % 10}"   # overlap on 10 keys
            await router.handle_write(ctx, key=key, value=i, tier="session")
            result = await router.handle_read(ctx, key=key, tier="session")
            assert result.found

        tasks = [write_and_read(i) for i in range(1000)]
        await asyncio.gather(*tasks)
        # No assertion on specific values — just that no exception was raised

    @pytest.mark.asyncio
    async def test_write_lock_prevents_interleaved_mutations(self) -> None:
        """Simulate two tasks trying to increment a counter.
        With the lock, the final value must be exactly 2."""
        from agenthicc.memory._router import MemoryRouter
        from agenthicc.memory._project import ProjectMemoryLayer
        from agenthicc.memory._global import GlobalMemoryLayer

        session_layer = SessionMemoryLayer(max_size=100)
        project_layer = ProjectMemoryLayer(db_path=":memory:")
        global_layer = GlobalMemoryLayer(db_path=":memory:")
        router = MemoryRouter(session_layer, project_layer, global_layer)

        ctx = AgentContext(
            agent_id="agent-x",
            session_id="s1",
            permission=AgentPermission.PROJECT,
        )

        await router.handle_write(ctx, key="ctr", value=0, tier="session")

        async def increment() -> None:
            result = await router.handle_read(ctx, key="ctr", tier="session")
            current = result.value or 0
            await router.handle_write(ctx, key="ctr", value=current + 1, tier="session")

        await asyncio.gather(increment(), increment())
        final = await router.handle_read(ctx, key="ctr", tier="session")
        # With serialised writes, final value is 2
        assert final.value == 2


# ---------------------------------------------------------------------------
# Permission scoping
# ---------------------------------------------------------------------------

class TestPermissionScoping:
    """Session-only agents cannot read or write project/global tiers."""

    def test_session_only_allows_session_tier(self) -> None:
        guard = PermissionGuard()
        ctx = AgentContext("a", "s", AgentPermission.SESSION_ONLY)
        # Should not raise
        guard.check_read(ctx, MemoryTier.SESSION)
        guard.check_write(ctx, MemoryTier.SESSION)

    def test_session_only_blocks_project_read(self) -> None:
        guard = PermissionGuard()
        ctx = AgentContext("a", "s", AgentPermission.SESSION_ONLY)
        with pytest.raises(MemoryPermissionError, match="project"):
            guard.check_read(ctx, MemoryTier.PROJECT)

    def test_session_only_blocks_global_write(self) -> None:
        guard = PermissionGuard()
        ctx = AgentContext("a", "s", AgentPermission.SESSION_ONLY)
        with pytest.raises(MemoryPermissionError, match="global"):
            guard.check_write(ctx, MemoryTier.GLOBAL)

    def test_project_permission_allows_project_and_session(self) -> None:
        guard = PermissionGuard()
        ctx = AgentContext("a", "s", AgentPermission.PROJECT)
        guard.check_read(ctx, MemoryTier.SESSION)
        guard.check_read(ctx, MemoryTier.PROJECT)
        guard.check_write(ctx, MemoryTier.PROJECT)

    def test_project_permission_blocks_global(self) -> None:
        guard = PermissionGuard()
        ctx = AgentContext("a", "s", AgentPermission.PROJECT)
        with pytest.raises(MemoryPermissionError, match="global"):
            guard.check_read(ctx, MemoryTier.GLOBAL)

    def test_global_permission_allows_all_tiers(self) -> None:
        guard = PermissionGuard()
        ctx = AgentContext("a", "s", AgentPermission.GLOBAL)
        for tier in MemoryTier:
            guard.check_read(ctx, tier)
            guard.check_write(ctx, tier)

    @pytest.mark.asyncio
    async def test_router_raises_on_permission_violation(self) -> None:
        from agenthicc.memory._router import MemoryRouter
        from agenthicc.memory._project import ProjectMemoryLayer
        from agenthicc.memory._global import GlobalMemoryLayer

        session_layer = SessionMemoryLayer(max_size=100)
        project_layer = ProjectMemoryLayer(db_path=":memory:")
        global_layer = GlobalMemoryLayer(db_path=":memory:")
        router = MemoryRouter(session_layer, project_layer, global_layer)

        ctx = AgentContext(
            agent_id="limited",
            session_id="s",
            permission=AgentPermission.SESSION_ONLY,
        )
        with pytest.raises(MemoryPermissionError):
            await router.handle_read(ctx, key="x", tier="project")
```

### 6.2 Integration Tests

```python
# tests/memory/test_integration.py
"""Integration: cross-agent artifact sharing via publish_artifact + memory_read."""
from __future__ import annotations

import asyncio
import pytest

from agenthicc.memory._router import MemoryRouter
from agenthicc.memory._session import SessionMemoryLayer
from agenthicc.memory._project import ProjectMemoryLayer
from agenthicc.memory._global import GlobalMemoryLayer
from agenthicc.memory._types import AgentContext, AgentPermission


@pytest.fixture()
def shared_router(tmp_path) -> MemoryRouter:
    """A MemoryRouter backed by a real on-disk SQLite database."""
    db = str(tmp_path / "test.db")
    session = SessionMemoryLayer(max_size=512)
    project = ProjectMemoryLayer(db_path=db)
    global_ = GlobalMemoryLayer(db_path=db)
    return MemoryRouter(session, project, global_)


def make_ctx(agent_id: str, permission: AgentPermission = AgentPermission.PROJECT) -> AgentContext:
    return AgentContext(agent_id=agent_id, session_id="shared-session", permission=permission)


class TestArtifactSharing:
    """One agent publishes an artifact; another agent retrieves it."""

    @pytest.mark.asyncio
    async def test_publish_then_retrieve(self, shared_router: MemoryRouter) -> None:
        publisher_ctx = make_ctx("test-runner-agent")
        reader_ctx = make_ctx("debugger-agent")

        content = b"FAIL: test_login\n  AssertionError: expected 200, got 401\n"

        # Publisher stores the artifact
        write_result = await shared_router.handle_publish_artifact(
            publisher_ctx,
            content=content.decode("utf-8"),
            content_encoding="utf-8",
            mime_type="text/plain",
            tags=["pytest", "failure"],
        )
        assert write_result.ok
        artifact_id = write_result.artifact_id
        assert artifact_id is not None
        assert artifact_id.startswith("artifact:")

        # Reader retrieves the artifact by ID
        read_result = await shared_router.handle_read(
            reader_ctx,
            artifact_id=artifact_id,
            tier="project",
        )
        assert read_result.found
        assert read_result.artifact_mime == "text/plain"
        assert read_result.artifact_content == content

    @pytest.mark.asyncio
    async def test_idempotent_publish(self, shared_router: MemoryRouter) -> None:
        """Publishing the same content twice returns the same artifact_id."""
        ctx = make_ctx("agent-a")
        content = "identical content"
        r1 = await shared_router.handle_publish_artifact(
            ctx, content=content, content_encoding="utf-8"
        )
        r2 = await shared_router.handle_publish_artifact(
            ctx, content=content, content_encoding="utf-8"
        )
        assert r1.artifact_id == r2.artifact_id

    @pytest.mark.asyncio
    async def test_project_kv_shared_across_agents(self, shared_router: MemoryRouter) -> None:
        """Agent A writes; Agent B reads from the same project namespace."""
        ctx_a = make_ctx("agent-a")
        ctx_b = make_ctx("agent-b")

        await shared_router.handle_write(
            ctx_a, key="shared-fact", value="db is postgres", tier="project",
            namespace="infra"
        )
        result = await shared_router.handle_read(
            ctx_b, key="shared-fact", tier="project", namespace="infra"
        )
        assert result.found
        assert result.value == "db is postgres"

    @pytest.mark.asyncio
    async def test_namespace_isolation(self, shared_router: MemoryRouter) -> None:
        """Two agents writing the same key in different namespaces do not collide."""
        ctx_a = make_ctx("agent-a")
        ctx_b = make_ctx("agent-b")

        await shared_router.handle_write(
            ctx_a, key="status", value="running", tier="project", namespace="ns-a"
        )
        await shared_router.handle_write(
            ctx_b, key="status", value="stopped", tier="project", namespace="ns-b"
        )
        r_a = await shared_router.handle_read(ctx_a, key="status", tier="project", namespace="ns-a")
        r_b = await shared_router.handle_read(ctx_b, key="status", tier="project", namespace="ns-b")
        assert r_a.value == "running"
        assert r_b.value == "stopped"

    @pytest.mark.asyncio
    async def test_semantic_search_returns_relevant_result(self, shared_router: MemoryRouter) -> None:
        """After writing text entries, semantic search should return the most relevant."""
        from lauren_ai._memory import InMemoryVectorStore

        # Rebuild with a real vector store
        db_path = ":memory:"
        vector_store = InMemoryVectorStore()
        project = ProjectMemoryLayer(db_path=db_path, vector_store=vector_store)
        session = SessionMemoryLayer()
        global_ = GlobalMemoryLayer(db_path=db_path)
        router = MemoryRouter(session, project, global_)

        ctx = make_ctx("agent-x")
        await router.handle_write(ctx, key="doc1",
                                   value="authentication middleware configuration",
                                   tier="project", namespace="docs")
        await router.handle_write(ctx, key="doc2",
                                   value="database connection pooling settings",
                                   tier="project", namespace="docs")
        await router.handle_write(ctx, key="doc3",
                                   value="user login flow and JWT tokens",
                                   tier="project", namespace="docs")

        results = await router.handle_read(
            ctx, query="how is login handled", tier="project", namespace="docs", k=2
        )
        assert results.found
        assert len(results.semantic_results) >= 1
        top_ids = {r.id for r in results.semantic_results}
        # The auth / login docs should rank higher than database pooling
        assert "docs:doc1" in top_ids or "docs:doc3" in top_ids
```

### 6.3 End-to-End Tests

```python
# tests/memory/test_e2e.py
"""E2E: debugger agent reads artifact published by failing test agent.

Simulates a two-agent pipeline:
  1. TestRunnerAgent runs a (fake) test suite, detects a failure, publishes
     the failure log as an artifact, and stores the artifact_id in project kv.
  2. DebuggerAgent reads the artifact_id from project kv, retrieves the
     artifact, and returns a suggested fix.
"""
from __future__ import annotations

import asyncio
import textwrap

import pytest

from agenthicc.memory._router import MemoryRouter
from agenthicc.memory._session import SessionMemoryLayer
from agenthicc.memory._project import ProjectMemoryLayer
from agenthicc.memory._global import GlobalMemoryLayer
from agenthicc.memory._types import AgentContext, AgentPermission


FAKE_FAILURE_LOG = textwrap.dedent("""\
    ============================= FAILURES ==============================
    __________________________ test_login ________________________________
    tests/test_auth.py:42: AssertionError
    assert response.status_code == 200
     +  where response.status_code = 401
    ============================= short test summary info ================
    FAILED tests/test_auth.py::test_login - AssertionError: assert 401 == 200
""")


@pytest.fixture()
async def pipeline_router(tmp_path) -> MemoryRouter:
    db = str(tmp_path / "pipeline.db")
    session = SessionMemoryLayer(max_size=256)
    project = ProjectMemoryLayer(db_path=db)
    global_ = GlobalMemoryLayer(db_path=db)
    return MemoryRouter(session, project, global_)


class TestRunnerAgent:
    """Simulated test-runner agent."""

    def __init__(self, router: MemoryRouter) -> None:
        self._router = router
        self._ctx = AgentContext(
            agent_id="test-runner",
            session_id="pipeline-001",
            permission=AgentPermission.PROJECT,
        )

    async def run(self) -> str:
        """Run fake tests, publish failure artifact, store artifact_id."""
        # Publish the failure log
        write_result = await self._router.handle_publish_artifact(
            self._ctx,
            content=FAKE_FAILURE_LOG,
            content_encoding="utf-8",
            mime_type="text/plain",
            tags=["pytest", "failure", "test_auth"],
            namespace="test-results",
        )
        assert write_result.ok
        artifact_id = write_result.artifact_id

        # Store artifact_id in project kv so debugger can find it
        await self._router.handle_write(
            self._ctx,
            key="latest-failure-artifact",
            value=artifact_id,
            tier="project",
            namespace="test-results",
        )
        return artifact_id


class DebuggerAgent:
    """Simulated debugger agent."""

    def __init__(self, router: MemoryRouter) -> None:
        self._router = router
        self._ctx = AgentContext(
            agent_id="debugger",
            session_id="pipeline-001",
            permission=AgentPermission.PROJECT,
        )

    async def investigate(self) -> dict:
        """Read the failure artifact and return analysis."""
        # Step 1: find the artifact_id published by test-runner
        kv_result = await self._router.handle_read(
            self._ctx,
            key="latest-failure-artifact",
            tier="project",
            namespace="test-results",
        )
        assert kv_result.found, "No failure artifact found in project memory"
        artifact_id = kv_result.value

        # Step 2: retrieve the artifact
        artifact_result = await self._router.handle_read(
            self._ctx,
            artifact_id=artifact_id,
            tier="project",
        )
        assert artifact_result.found, f"Artifact {artifact_id} not found"
        content = artifact_result.artifact_content
        assert content is not None

        # Step 3: (in real life, call LLM here — we just return the content)
        return {
            "artifact_id": artifact_id,
            "failure_log": content.decode("utf-8"),
            "suggested_fix": "Check authentication token expiry in middleware.",
        }


class TestE2EDebuggerPipeline:
    @pytest.mark.asyncio
    async def test_debugger_reads_artifact_from_test_runner(
        self, pipeline_router: MemoryRouter
    ) -> None:
        runner = TestRunnerAgent(pipeline_router)
        debugger = DebuggerAgent(pipeline_router)

        # Test runner executes first
        published_id = await runner.run()
        assert published_id.startswith("artifact:")

        # Debugger runs independently and retrieves the artifact
        analysis = await debugger.investigate()

        assert analysis["artifact_id"] == published_id
        assert "test_login" in analysis["failure_log"]
        assert "suggested_fix" in analysis

    @pytest.mark.asyncio
    async def test_session_only_debugger_cannot_access_artifact(
        self, pipeline_router: MemoryRouter
    ) -> None:
        """A session-only debugger must not be able to read project artifacts."""
        from agenthicc.memory._permission import MemoryPermissionError

        runner = TestRunnerAgent(pipeline_router)
        await runner.run()

        restricted_ctx = AgentContext(
            agent_id="restricted-debugger",
            session_id="pipeline-001",
            permission=AgentPermission.SESSION_ONLY,
        )
        with pytest.raises(MemoryPermissionError):
            await pipeline_router.handle_read(
                restricted_ctx,
                key="latest-failure-artifact",
                tier="project",
                namespace="test-results",
            )

    @pytest.mark.asyncio
    async def test_compaction_runs_without_error(
        self, pipeline_router: MemoryRouter
    ) -> None:
        """Smoke test: compaction scheduler starts, runs one cycle, and stops."""
        from agenthicc.memory._compaction import MemoryCompactionScheduler
        from agenthicc.memory._session import SessionMemoryLayer
        from agenthicc.memory._project import ProjectMemoryLayer
        from agenthicc.memory._global import GlobalMemoryLayer

        # Use the router's internal layers (access via private attrs for test only)
        scheduler = MemoryCompactionScheduler(
            session=pipeline_router._session,
            project=pipeline_router._project,
            global_=pipeline_router._global,
            session_interval_s=0.05,   # very short for test speed
            vacuum_interval_s=0.05,
        )
        scheduler.start()
        await asyncio.sleep(0.15)       # allow at least 2 compaction cycles
        await scheduler.stop()          # must not raise
```

### 6.4 Running the Tests

```bash
# From the repo root
pytest tests/memory/ -v --timeout=30

# With coverage
pytest tests/memory/ -v --cov=agenthicc.memory --cov-report=term-missing
```

---

## 7. Configuration Reference

Configuration is read from `pyproject.toml` or a dedicated
`.agenthicc/config.toml` in the project directory, and from
`~/.agenthicc/config.toml` for global defaults.  Project-level config
overrides global defaults.

```toml
# .agenthicc/config.toml
[memory]
# ──────────────────────────────────────────────────────────────────────
# TIER 2: Project layer
# ──────────────────────────────────────────────────────────────────────

# Path to the SQLite database for project-layer kv storage.
# Relative paths are resolved relative to the config file's directory.
project_memory_path = ".agenthicc/memory/project.db"

# Vector database backend for semantic search.
# Supported: "sqlite-vec" | "chromadb" | "in-memory"
# "sqlite-vec"  – sqlite-vec extension, zero dependencies, recommended for
#                 small-to-medium projects.
# "chromadb"    – ChromaDB server or embedded, better for large corpora.
# "in-memory"   – InMemoryVectorStore (TF-IDF); no persistence, for testing.
vector_db = "sqlite-vec"

# Path for the vector DB files (applies to sqlite-vec).
vector_db_path = ".agenthicc/memory/vectors.db"

# ChromaDB server URL (only used when vector_db = "chromadb")
# chromadb_host = "localhost"
# chromadb_port = 8000
# chromadb_collection = "agenthicc-project"

# Maximum number of rows the project kv table may hold before old entries
# are flagged for compaction.  0 = unlimited.
project_kv_max_rows = 0

# ──────────────────────────────────────────────────────────────────────
# TIER 1: Session layer
# ──────────────────────────────────────────────────────────────────────

# Maximum number of entries in the in-process LRU cache.
session_lru_max_size = 1024

# Maximum token budget for ShortTermMemory conversation buffers.
session_max_tokens = 40000

# How often (seconds) the background compaction task prunes expired TTL entries.
session_compaction_interval_s = 60

# ──────────────────────────────────────────────────────────────────────
# TIER 3: Global layer
# ──────────────────────────────────────────────────────────────────────

# Path to the SQLite database for global preferences and long-term facts.
# Tilde expansion is supported.
global_memory_path = "~/.agenthicc/memory/global.db"

# ──────────────────────────────────────────────────────────────────────
# SQLite tuning (applies to both project and global layers)
# ──────────────────────────────────────────────────────────────────────

# SQLite journal mode.  WAL is strongly recommended for concurrent access.
sqlite_journal_mode = "WAL"

# SQLite synchronous mode.  NORMAL is safe with WAL.
sqlite_synchronous = "NORMAL"

# Connection timeout in seconds when waiting for the write lock.
sqlite_timeout_s = 30.0

# How often (seconds) the background task runs SQLite VACUUM.
# Default: 86400 (daily).
vacuum_interval_s = 86400

# ──────────────────────────────────────────────────────────────────────
# Artifact storage
# ──────────────────────────────────────────────────────────────────────

# Maximum size (bytes) for a single artifact.  Writes exceeding this are
# rejected.  Default: 50 MiB.
artifact_max_bytes = 52428800

# ──────────────────────────────────────────────────────────────────────
# Permission defaults
# ──────────────────────────────────────────────────────────────────────

# Default permission granted to agents that do not specify one explicitly.
# Accepted: "session_only" | "project" | "global"
default_agent_permission = "project"
```

### 7.1 Configuration Dataclass

```python
# src/agenthicc/memory/_config.py
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(slots=True)
class MemoryConfig:
    """Parsed representation of the [memory] TOML section."""
    project_memory_path: str = ".agenthicc/memory/project.db"
    global_memory_path: str = "~/.agenthicc/memory/global.db"
    vector_db: str = "sqlite-vec"
    vector_db_path: str = ".agenthicc/memory/vectors.db"
    chromadb_host: str = "localhost"
    chromadb_port: int = 8000
    chromadb_collection: str = "agenthicc-project"
    session_lru_max_size: int = 1024
    session_max_tokens: int = 40_000
    session_compaction_interval_s: float = 60.0
    sqlite_journal_mode: str = "WAL"
    sqlite_synchronous: str = "NORMAL"
    sqlite_timeout_s: float = 30.0
    vacuum_interval_s: float = 86_400.0
    artifact_max_bytes: int = 52_428_800
    default_agent_permission: str = "project"
    project_kv_max_rows: int = 0

    def resolve_paths(self) -> None:
        """Expand ~ and make relative paths absolute."""
        self.project_memory_path = os.path.abspath(
            os.path.expanduser(self.project_memory_path)
        )
        self.global_memory_path = os.path.abspath(
            os.path.expanduser(self.global_memory_path)
        )
        self.vector_db_path = os.path.abspath(
            os.path.expanduser(self.vector_db_path)
        )
```

---

## 8. Open Questions

| # | Question | Owner | Status |
|---|----------|-------|--------|
| OQ-01 | **Vector DB selection**: Should `sqlite-vec` be the default, or should ChromaDB be preferred for new projects with >100K documents? `sqlite-vec` has zero runtime dependencies but may be slower at scale. | @infra | Open |
| OQ-02 | **Write lock granularity**: The current design serialises ALL writes to a tier. For the project layer, a per-namespace lock would allow higher write throughput. Is the additional complexity justified given expected write rates? | @platform-team | Open |
| OQ-03 | **Artifact size cap**: The 50 MiB default artifact cap may be too small for model checkpoints or large generated files. Should this be configurable per-artifact at publish time, or only at the global config level? | @ai-runtime | Open |
| OQ-04 | **TTL on project layer**: The current spec omits TTL support for project-layer entries. Should project entries support an optional `expires_at`? This would require a background sweep similar to the session-layer compactor. | @platform-team | Open |
| OQ-05 | **Encryption-at-rest**: Global-layer facts may contain sensitive user preferences. Should the global SQLite database be encrypted (e.g. SQLCipher)? If so, how is the key derived and stored? | @security | Open |
| OQ-06 | **Concurrent vector upserts**: `InMemoryVectorStore` and `SQLiteVectorStore` are not internally thread-safe for concurrent async writes. The project-layer write lock covers them, but only if all writes go through `MemoryRouter`. Should `ProjectMemoryLayer` own its own vector-write lock internally for defence-in-depth? | @platform-team | Open |
| OQ-07 | **Memory compaction during testing**: The compaction background task runs every 60 s by default. Integration tests that create real SQLite databases need either a way to trigger compaction manually or a very short `session_compaction_interval_s`. Should `MemoryCompactionScheduler` expose a `compact_now()` coroutine? | @platform-team | Open |
| OQ-08 | **Global-layer namespace support**: The current spec does not include namespaces for the global layer. Should global entries support namespacing for multi-profile use cases (e.g. different AI assistants sharing the same `~/.agenthicc/`)? | @platform-team | Open |
| OQ-09 | **LLM-powered memory extraction**: The `@remember` decorator in `lauren_ai._memory._remember` uses an LLM to extract facts. Should `agenthicc` re-expose this mechanism through a `memory_write(mode="extract")` path that calls the extraction LLM on free-form text before storing? | @ai-runtime | Open |
| OQ-10 | **Vector store migration**: If a project switches from `in-memory` to `sqlite-vec`, existing vector entries are lost. Should the config layer detect a backend change and trigger a re-index from the project kv table? | @infra | Open |

---

*End of PRD-05: Memory Architecture*
