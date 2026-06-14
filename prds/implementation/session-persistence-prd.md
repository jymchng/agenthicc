# Session Persistence — Implementation PRD

**Document type**: Implementation specification  
**Status**: Final  
**Date**: 2026-06-13  
**Scope**: Complete session persistence system for AgentHICC, covering data models,
storage architecture, session lifecycle, crash recovery, import/export, and the full
test specification.

---

## 0. Dependency Map

This document depends on the following existing modules (read before writing any code):

| Module | Path | Key symbols consumed |
|--------|------|----------------------|
| `ConversationStore` | `src/agenthicc/conversation_store.py` | `ConversationStore`, `_default_db_path` |
| `ProjectMemoryLayer` | `src/agenthicc/memory/layers.py` | `ProjectMemoryLayer`, `ArtifactRecord` |
| `GlobalMemoryLayer` | `src/agenthicc/memory/layers.py` | `GlobalMemoryLayer` |
| `__main__` session helpers | `src/agenthicc/__main__.py` | `_SESSIONS_DIR`, `_SESSION_INDEX`, `_register_session`, `_touch_session`, `_find_latest_session_for_cwd`, `_load_session_index`, `_save_session_index` |
| `EventProcessor` | `src/agenthicc/kernel/processor.py` | `EventProcessor`, `restore_from_log` |
| `AppState` | `src/agenthicc/kernel/state.py` | `AppState` |
| `AgenthiccConfig` | `src/agenthicc/config.py` | `AgenthiccConfig`, `load_config` |

Hard constraints carried over from master PRD and CLAUDE.md:

- No alternate-screen usage.
- Python 3.10+, `from __future__ import annotations` on every source file.
- All public types must be mypy-clean (no implicit `Any`, no untyped defs).
- `asyncio_mode = "auto"` in pytest — no `@pytest.mark.asyncio` decorators.
- `ruff` line-length 100.
- SQLite WAL mode throughout; no external dependencies for storage.
- Must integrate with `ConversationStore` (extends its schema, does not replace it)
  and with `ProjectMemoryLayer` (reuses its `db_path` pattern).

---

## 1. Session Data Model

### 1.1 SessionMetadata

Location: `src/agenthicc/session/models.py`

```python
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "SessionMetadata",
    "SessionIndexEntry",
    "TurnRecord",
    "ToolCallRecord",
    "SessionExport",
    "IncompleteMarker",
]


@dataclass
class SessionMetadata:
    """Full metadata for one session, stored in session.json."""

    session_id: str                   # UUID hex, 32 chars
    created_at: float                 # Unix timestamp (time.time())
    updated_at: float                 # Unix timestamp, updated after every turn
    turn_count: int                   # number of completed Q+A pairs
    model_id: str                     # e.g. "anthropic/claude-sonnet-4-6"
    cwd: str                          # absolute working directory at session start
    total_cost_usd: float             # cumulative cost, rounded to 6 decimal places
    total_input_tokens: int           # cumulative input tokens
    total_output_tokens: int          # cumulative output tokens
    tags: list[str] = field(default_factory=list)  # user-supplied labels
    incomplete: bool = False          # True if session ended without clean shutdown
    incomplete_at: float | None = None  # Unix timestamp of last incomplete marker write

    @classmethod
    def new(cls, cwd: str, model_id: str) -> "SessionMetadata":
        now = time.time()
        return cls(
            session_id=uuid.uuid4().hex,
            created_at=now,
            updated_at=now,
            turn_count=0,
            model_id=model_id,
            cwd=cwd,
            total_cost_usd=0.0,
            total_input_tokens=0,
            total_output_tokens=0,
        )

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMetadata":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
```

**Constraints:**

- `session_id` is always a 32-character hex string (UUID4 without hyphens).
- `updated_at` is written after every `save_turn()` call and after every
  `save_memory_snapshot()` call. It is NOT updated on every kernel event — only on
  user-visible turn boundaries.
- `incomplete` is set to `True` on every session start and cleared to `False` only
  on clean `close()`. A session found with `incomplete=True` at startup was not
  cleanly closed (crash or SIGKILL).
- `tags` is an empty list by default; users can tag sessions via `/tag <label>`.
- All float fields use Python float (IEEE 754 double); precision is sufficient for
  cost tracking at 6 decimal places over thousands of turns.

### 1.2 SessionIndexEntry

The global session index maps `session_id` to a lightweight summary for fast listing
without reading individual session directories.

```python
@dataclass
class SessionIndexEntry:
    """One row in session-index.json. Kept small for fast listing."""

    session_id: str
    cwd: str
    created_at: float
    updated_at: float
    turn_count: int
    model_id: str
    total_cost_usd: float
    tags: list[str]
    incomplete: bool

    @classmethod
    def from_metadata(cls, meta: SessionMetadata) -> "SessionIndexEntry":
        return cls(
            session_id=meta.session_id,
            cwd=meta.cwd,
            created_at=meta.created_at,
            updated_at=meta.updated_at,
            turn_count=meta.turn_count,
            model_id=meta.model_id,
            total_cost_usd=meta.total_cost_usd,
            tags=meta.tags,
            incomplete=meta.incomplete,
        )
```

**File location**: `~/.agenthicc/session-index.json`

The global index lives in the user home `.agenthicc` directory, NOT inside any project
directory. This allows `agenthicc sessions` to list sessions across all projects
without requiring access to each project's directory.

**File format**: A single JSON object:

```json
{
  "version": 1,
  "sessions": {
    "<session_id>": {
      "session_id": "abc123...",
      "cwd": "/home/user/myproject",
      "created_at": 1749812400.0,
      "updated_at": 1749816000.0,
      "turn_count": 23,
      "model_id": "anthropic/claude-sonnet-4-6",
      "total_cost_usd": 0.045231,
      "tags": ["incident", "prod-db"],
      "incomplete": false
    }
  }
}
```

**Write strategy**: The index is read-modify-write with an OS-level rename for
atomicity:

```python
import json, os
from pathlib import Path

def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))   # atomic on POSIX
```

This means a concurrent write from two simultaneous agenthicc sessions in different
projects is last-write-wins for the index file (acceptable for the single-user use
case; each session only writes its own entry).

### 1.3 TurnRecord and ToolCallRecord

```python
@dataclass
class ToolCallRecord:
    """One tool invocation within a turn."""

    tool_use_id: str         # opaque ID from LLM response (lauren-ai ToolCallStarted.tool_use_id)
    tool_name: str           # e.g. "read_file", "run_bash"
    args: dict               # JSON-serialisable kwargs passed to the tool
    state: str               # "success" | "error" | "rejected"
    result_summary: str      # short human-readable result (first line of output, max 200 chars)
    duration_ms: float       # wall-clock time
    error_message: str       # non-empty only when state == "error" | "rejected"
    diff: str                # unified diff produced for file-editing tools (may be "")


@dataclass
class TurnRecord:
    """One completed Q+A pair stored in the ConversationStore turns table."""

    turn_id: int                              # auto-increment, unique within session
    session_id: str
    role: Literal["user", "assistant", "tool"]
    content: str                              # markdown text for user/assistant; JSON for tool
    timestamp: float                          # Unix timestamp
    model_short: str | None                   # e.g. "claude-sonnet-4-6" (None for user role)
    tool_calls: list[ToolCallRecord]          # empty for user/assistant roles
    cost_usd: float                           # 0.0 for user role
    input_tokens: int                         # 0 for user role
    output_tokens: int                        # 0 for user role
```

`TurnRecord` is the in-memory representation. The SQLite schema in section 2.1 is
the durable representation. They map 1:1.

`tool_calls` for the `assistant` role contains all tool calls made during that
LLM turn. For the `tool` role (rare; used when the content is raw tool output
committed separately), `tool_calls` is empty.

**Content encoding:**

- `role="user"`: `content` is the raw user input string.
- `role="assistant"`: `content` is the final rendered markdown text (Markdown
  sentinel stripped, same as what `conv_store.save_turn()` currently writes).
- `role="tool"`: `content` is a JSON array `[{"tool_name": ..., "result": ...}]`.
  This role is OPTIONAL and only written when the agent explicitly wants tool output
  visible in the session transcript outside of an assistant turn.

`tool_calls` is serialised as a JSON array in the SQLite `tool_calls_json` column
and deserialised on read.

---

## 2. Storage Architecture

### 2.1 ConversationStore Schema Extension

The existing `ConversationStore` (`src/agenthicc/conversation_store.py`) has a `turns`
table and a `memory_snapshots` table. This PRD extends it with three additional tables.
The extension is backward-compatible: existing databases gain the new tables via a
`CREATE TABLE IF NOT EXISTS` migration run on every `ConversationStore.__init__()` call.

**Do not rename or alter existing columns in `turns` or `memory_snapshots`.**
New columns are added with `ALTER TABLE ... ADD COLUMN ... DEFAULT ...` to preserve
backward compatibility.

#### 2.1.1 Extended `turns` table

The existing schema is:

```sql
CREATE TABLE IF NOT EXISTS turns (
    session_id  TEXT    NOT NULL,
    turn_index  INTEGER NOT NULL,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    timestamp   REAL    NOT NULL,
    model_short TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, turn_index, role)
);
```

Add these columns via migration (applied in `ConversationStore._migrate()` called
from `__init__()`):

```sql
-- Migration v2: add token counts, cost, and tool call metadata
ALTER TABLE turns ADD COLUMN input_tokens  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN cost_usd      REAL    NOT NULL DEFAULT 0.0;
ALTER TABLE turns ADD COLUMN tool_calls_json TEXT   NOT NULL DEFAULT '[]';
```

After migration the full DDL (for new databases) is:

```sql
CREATE TABLE IF NOT EXISTS turns (
    session_id       TEXT    NOT NULL,
    turn_index       INTEGER NOT NULL,
    role             TEXT    NOT NULL,
    content          TEXT    NOT NULL,
    timestamp        REAL    NOT NULL,
    model_short      TEXT    NOT NULL DEFAULT '',
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0.0,
    tool_calls_json  TEXT    NOT NULL DEFAULT '[]',
    PRIMARY KEY (session_id, turn_index, role)
);
```

#### 2.1.2 New `session_metadata` table

```sql
CREATE TABLE IF NOT EXISTS session_metadata (
    session_id          TEXT  PRIMARY KEY,
    created_at          REAL  NOT NULL,
    updated_at          REAL  NOT NULL,
    turn_count          INTEGER NOT NULL DEFAULT 0,
    model_id            TEXT  NOT NULL DEFAULT '',
    cwd                 TEXT  NOT NULL DEFAULT '',
    total_cost_usd      REAL  NOT NULL DEFAULT 0.0,
    total_input_tokens  INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    tags_json           TEXT  NOT NULL DEFAULT '[]',
    incomplete          INTEGER NOT NULL DEFAULT 0,   -- 0=false, 1=true (SQLite has no bool)
    incomplete_at       REAL,                          -- NULL when incomplete=0
    metadata_json       TEXT  NOT NULL DEFAULT '{}'   -- future extensibility
);
```

#### 2.1.3 New `turn_summaries` table

Stores a one-line AI-generated or template-generated summary of each turn for use in
session recap display without loading full content.

```sql
CREATE TABLE IF NOT EXISTS turn_summaries (
    session_id  TEXT    NOT NULL,
    turn_index  INTEGER NOT NULL,
    summary     TEXT    NOT NULL,
    PRIMARY KEY (session_id, turn_index)
);
```

#### 2.1.4 New `session_tags` table

Normalized tag lookup (in addition to the JSON array in `session_metadata`). This
allows efficient `WHERE tag = 'incident'` queries at the cost of one extra table.

```sql
CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (session_id, tag),
    FOREIGN KEY (session_id) REFERENCES session_metadata(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);
```

#### 2.1.5 Index strategy

```sql
-- Fast resume: most-recent session for a given cwd
CREATE INDEX IF NOT EXISTS idx_sm_cwd_updated ON session_metadata(cwd, updated_at DESC);

-- Fast listing: all sessions ordered by recency
CREATE INDEX IF NOT EXISTS idx_sm_updated ON session_metadata(updated_at DESC);

-- Fast turn replay: all turns for a session in order
CREATE INDEX IF NOT EXISTS idx_turns_session_idx ON turns(session_id, turn_index);

-- Fast conversation history lookups
CREATE INDEX IF NOT EXISTS idx_turns_role ON turns(session_id, role, turn_index);
```

#### 2.1.6 Migration strategy

The migration logic lives in `ConversationStore._migrate()`. It is idempotent and
runs on every `__init__()`. The approach is a `user_version` pragma:

```python
def _migrate(self) -> None:
    """Apply schema migrations idempotently using PRAGMA user_version."""
    current_version: int = self._conn.execute("PRAGMA user_version").fetchone()[0]

    if current_version < 1:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS turns (
                session_id       TEXT    NOT NULL,
                turn_index       INTEGER NOT NULL,
                role             TEXT    NOT NULL,
                content          TEXT    NOT NULL,
                timestamp        REAL    NOT NULL,
                model_short      TEXT    NOT NULL DEFAULT '',
                input_tokens     INTEGER NOT NULL DEFAULT 0,
                output_tokens    INTEGER NOT NULL DEFAULT 0,
                cost_usd         REAL    NOT NULL DEFAULT 0.0,
                tool_calls_json  TEXT    NOT NULL DEFAULT '[]',
                PRIMARY KEY (session_id, turn_index, role)
            );
            CREATE TABLE IF NOT EXISTS memory_snapshots (
                session_id    TEXT NOT NULL PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                updated_at    REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_metadata (
                session_id          TEXT  PRIMARY KEY,
                created_at          REAL  NOT NULL,
                updated_at          REAL  NOT NULL,
                turn_count          INTEGER NOT NULL DEFAULT 0,
                model_id            TEXT  NOT NULL DEFAULT '',
                cwd                 TEXT  NOT NULL DEFAULT '',
                total_cost_usd      REAL  NOT NULL DEFAULT 0.0,
                total_input_tokens  INTEGER NOT NULL DEFAULT 0,
                total_output_tokens INTEGER NOT NULL DEFAULT 0,
                tags_json           TEXT  NOT NULL DEFAULT '[]',
                incomplete          INTEGER NOT NULL DEFAULT 0,
                incomplete_at       REAL,
                metadata_json       TEXT  NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS turn_summaries (
                session_id  TEXT    NOT NULL,
                turn_index  INTEGER NOT NULL,
                summary     TEXT    NOT NULL,
                PRIMARY KEY (session_id, turn_index)
            );
            CREATE TABLE IF NOT EXISTS session_tags (
                session_id TEXT NOT NULL,
                tag        TEXT NOT NULL,
                PRIMARY KEY (session_id, tag),
                FOREIGN KEY (session_id) REFERENCES session_metadata(session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sm_cwd_updated   ON session_metadata(cwd, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sm_updated        ON session_metadata(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_turns_session_idx ON turns(session_id, turn_index);
            CREATE INDEX IF NOT EXISTS idx_turns_role        ON turns(session_id, role, turn_index);
            CREATE INDEX IF NOT EXISTS idx_session_tags_tag  ON session_tags(tag);
            PRAGMA user_version = 1;
        """)

    if current_version < 2:
        # Add new columns to existing databases (ALTER TABLE ADD COLUMN is safe)
        # SQLite will error if the column already exists — catch and continue.
        for stmt in [
            "ALTER TABLE turns ADD COLUMN input_tokens  INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE turns ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE turns ADD COLUMN cost_usd      REAL    NOT NULL DEFAULT 0.0",
            "ALTER TABLE turns ADD COLUMN tool_calls_json TEXT   NOT NULL DEFAULT '[]'",
        ]:
            try:
                self._conn.execute(stmt)
            except Exception:
                pass  # column already exists
        self._conn.execute("PRAGMA user_version = 2")
        self._conn.commit()
```

#### 2.1.7 Key queries

All queries are synchronous because `ConversationStore` uses a single long-lived
connection (same pattern as the existing implementation). For async callers, wrap
with `asyncio.to_thread`.

```python
# Create or update session metadata
UPSERT_SESSION_METADATA = """
INSERT INTO session_metadata
    (session_id, created_at, updated_at, turn_count, model_id, cwd,
     total_cost_usd, total_input_tokens, total_output_tokens, tags_json,
     incomplete, incomplete_at, metadata_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
ON CONFLICT(session_id) DO UPDATE SET
    updated_at          = excluded.updated_at,
    turn_count          = excluded.turn_count,
    total_cost_usd      = excluded.total_cost_usd,
    total_input_tokens  = excluded.total_input_tokens,
    total_output_tokens = excluded.total_output_tokens,
    tags_json           = excluded.tags_json,
    incomplete          = excluded.incomplete,
    incomplete_at       = excluded.incomplete_at
"""

# Load session metadata by session_id
SELECT_SESSION_METADATA = """
SELECT session_id, created_at, updated_at, turn_count, model_id, cwd,
       total_cost_usd, total_input_tokens, total_output_tokens, tags_json,
       incomplete, incomplete_at
FROM session_metadata
WHERE session_id = ?
"""

# List all sessions ordered by recency (for /history and --list-sessions)
LIST_SESSIONS_RECENT = """
SELECT session_id, cwd, updated_at, turn_count, model_id, total_cost_usd,
       tags_json, incomplete
FROM session_metadata
ORDER BY updated_at DESC
LIMIT ?
"""

# List sessions for a specific cwd
LIST_SESSIONS_FOR_CWD = """
SELECT session_id, cwd, updated_at, turn_count, model_id, total_cost_usd,
       tags_json, incomplete
FROM session_metadata
WHERE cwd = ?
ORDER BY updated_at DESC
"""

# Save extended turn (replaces existing save_turn)
UPSERT_TURN_EXTENDED = """
INSERT OR REPLACE INTO turns
    (session_id, turn_index, role, content, timestamp, model_short,
     input_tokens, output_tokens, cost_usd, tool_calls_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Load last N turns for replay
SELECT_LAST_N_TURNS = """
SELECT turn_index, role, content, timestamp, model_short,
       input_tokens, output_tokens, cost_usd, tool_calls_json
FROM turns
WHERE session_id = ?
ORDER BY turn_index DESC
LIMIT ?
"""

# Mark session incomplete (written at startup, cleared on clean close)
MARK_INCOMPLETE = """
UPDATE session_metadata
SET incomplete = 1, incomplete_at = ?
WHERE session_id = ?
"""

# Clear incomplete flag (written on clean close)
CLEAR_INCOMPLETE = """
UPDATE session_metadata
SET incomplete = 0, incomplete_at = NULL
WHERE session_id = ?
"""

# Delete session (for cleanup)
DELETE_SESSION_METADATA = "DELETE FROM session_metadata WHERE session_id = ?"
DELETE_SESSION_TURNS    = "DELETE FROM turns WHERE session_id = ?"
DELETE_SESSION_MEMORY   = "DELETE FROM memory_snapshots WHERE session_id = ?"
DELETE_SESSION_SUMMARIES= "DELETE FROM turn_summaries WHERE session_id = ?"
DELETE_SESSION_TAGS     = "DELETE FROM session_tags WHERE session_id = ?"
```

### 2.2 File Layout

The session persistence system uses two root directories:

1. **`~/.agenthicc/`** — user-global data shared across all projects:
   - `session-index.json` — global lightweight index of all sessions
   - `global.db` — `GlobalMemoryLayer` SQLite database (unchanged)
   - `conversation-stores/<cwd-hash>.db` — `ConversationStore` per project
     (existing path; this PRD adds tables to this database)

2. **`.agenthicc/sessions/<session_id>/`** — per-session directory within the
   project working directory:

```
~/.agenthicc/
  session-index.json        ← global index (SessionIndexEntry per session)
  global.db                 ← GlobalMemoryLayer (unchanged)
  conversation-stores/
    <sha256(cwd)[:16]>.db   ← ConversationStore per project (extended schema)

<project_cwd>/
  .agenthicc/
    sessions/
      <session_id>/
        session.json          ← SessionMetadata (full, including incomplete flag)
        events.jsonl          ← kernel event log (append-only, existing format)
        memory_snapshot.json  ← ShortTermMemory snapshot (JSON, updated per turn)
    sessions.json             ← per-project index (existing __main__.py format,
                                 kept for backward compat with existing session
                                 index helpers in __main__.py)
    snapshot.json             ← AppState snapshot (existing, unchanged)
    history                   ← readline history file (existing, unchanged)
    agenthicc.toml            ← project config (existing, unchanged)
```

**Critical path change**: `session.json` is the new canonical source of truth for
session metadata. The existing `sessions.json` (per-project) is kept for backward
compatibility with the `_load_session_index()` / `_save_session_index()` helpers in
`__main__.py` but is now a secondary index derived from `session.json` on write.

**Why two separate index files?**

- `~/.agenthicc/session-index.json` enables `agenthicc sessions` to list all
  sessions from all projects without requiring each project directory to be
  accessible (e.g., on a different machine after git clone).
- `.agenthicc/sessions.json` (existing) is kept so existing `--resume` / `--continue`
  CLI flags continue to work without any changes to the argument parsing in `__main__.py`.

### 2.3 Storage Limits and Cleanup Policy

#### Per-session disk limits

| Artifact | Soft limit | Hard limit | Enforcement |
|----------|-----------|------------|-------------|
| `events.jsonl` | 50 MB | 100 MB | Rotate to `events.jsonl.1` at 100 MB (atomic rename) |
| `memory_snapshot.json` | 2 MB | 5 MB | Truncate oldest conversation history entries if over limit |
| `session.json` | — | 64 KB | Session metadata is small; never truncated |
| SQLite `turns` rows | — | 10 000 per session | Error on `save_turn()` if limit exceeded (never expected in practice) |

Event log rotation (existing behaviour, codified):

```python
def _maybe_rotate_event_log(log_path: str) -> None:
    """Rotate events.jsonl to events.jsonl.1 if it exceeds 100 MB.
    
    Called before writing any new event line. Rotation is atomic: rename
    events.jsonl to events.jsonl.1, then start writing a new events.jsonl.
    A second rotation overwrites events.jsonl.1 (only one backup kept).
    """
    p = Path(log_path)
    if p.exists() and p.stat().st_size > 100 * 1024 * 1024:
        backup = p.with_suffix(".jsonl.1")
        os.replace(str(p), str(backup))
```

#### Global cleanup policy

When the total number of sessions in `~/.agenthicc/session-index.json` exceeds
`MAX_SESSIONS` (default: 500), `SessionManager.gc()` deletes the oldest sessions
(by `updated_at`) until the count is at or below `MAX_SESSIONS - 50`. Deletion
removes:

1. The `~/.agenthicc/session-index.json` entry.
2. The `.agenthicc/sessions/<id>/` directory tree (if still accessible).
3. The `session_metadata`, `turns`, `turn_summaries`, `session_tags`, and
   `memory_snapshots` rows from the `ConversationStore` database.

Sessions tagged as "important" (any non-empty `tags` list) are exempt from GC.
Incomplete sessions older than 30 days are always eligible for GC regardless of tags.

`gc()` is called lazily at session creation time if `len(index) > MAX_SESSIONS`.
It is never called at startup or shutdown (to avoid latency impact).

#### Compression

Sessions not accessed in the last 30 days may be compressed:

```python
def _compress_old_session(session_dir: Path) -> None:
    """Gzip-compress events.jsonl for sessions older than 30 days.
    
    Compressed file: events.jsonl.gz (events.jsonl deleted after successful compress).
    On resume, events.jsonl.gz is decompressed to events.jsonl before restore_from_log().
    """
```

Compression is opt-in, controlled by `memory.compress_old_sessions = true` in
`agenthicc.toml` (default: false). It is never applied to sessions with
`incomplete=True`.

---

## 3. Session Lifecycle

### 3.1 Session Manager

All session lifecycle operations go through a `SessionManager` class. This
centralizes the logic currently scattered across `__main__.py` helper functions.

Location: `src/agenthicc/session/manager.py`

```python
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.session.models import SessionMetadata, SessionIndexEntry
    from agenthicc.conversation_store import ConversationStore

__all__ = ["SessionManager"]

MAX_SESSIONS = 500


class SessionManager:
    """Centralises session creation, persistence, and lifecycle management.

    One SessionManager instance is created per TUI session and lives for the
    duration of the process. It owns the session directory, the session.json
    file, and the global index entry for this session.
    """

    def __init__(
        self,
        session_id: str,
        cwd: str,
        model_id: str,
        conv_store: "ConversationStore",
        sessions_dir: Path | None = None,
        global_index_path: Path | None = None,
    ) -> None:
        self._session_id = session_id
        self._cwd = cwd
        self._model_id = model_id
        self._conv_store = conv_store
        self._sessions_dir = sessions_dir or (Path(cwd) / ".agenthicc" / "sessions")
        self._session_dir = self._sessions_dir / session_id
        self._global_index_path = (
            global_index_path or Path.home() / ".agenthicc" / "session-index.json"
        )
        self._metadata: SessionMetadata | None = None
        self._closed: bool = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def events_log_path(self) -> Path:
        return self._session_dir / "events.jsonl"

    @property
    def memory_snapshot_path(self) -> Path:
        return self._session_dir / "memory_snapshot.json"

    @property
    def session_json_path(self) -> Path:
        return self._session_dir / "session.json"

    # ── creation ──────────────────────────────────────────────────────────

    def create(self) -> "SessionMetadata":
        """Create a new session: mkdir, session.json, index registration.

        Marks the session as incomplete immediately (cleared on close()).
        Must be called before any events are written.
        """
        from agenthicc.session.models import SessionMetadata  # noqa: PLC0415

        self._session_dir.mkdir(parents=True, exist_ok=True)
        meta = SessionMetadata.new(cwd=self._cwd, model_id=self._model_id)
        meta.session_id = self._session_id
        meta.incomplete = True
        meta.incomplete_at = time.time()
        self._metadata = meta
        self._write_session_json(meta)
        self._conv_store.upsert_session_metadata(meta)
        self._update_global_index(meta)
        self._update_project_index(meta)
        return meta

    def resume(self, session_id: str) -> "SessionMetadata | None":
        """Load an existing session for resume. Returns None if not found."""
        from agenthicc.session.models import SessionMetadata  # noqa: PLC0415

        session_json = self._sessions_dir / session_id / "session.json"
        if not session_json.exists():
            # Fall back to ConversationStore
            meta = self._conv_store.load_session_metadata(session_id)
            if meta is None:
                return None
        else:
            meta = SessionMetadata.from_dict(json.loads(session_json.read_text()))

        # Mark as incomplete again (we are now running it)
        meta.incomplete = True
        meta.incomplete_at = time.time()
        self._metadata = meta
        self._write_session_json(meta)
        self._conv_store.upsert_session_metadata(meta)
        self._update_global_index(meta)
        return meta

    # ── auto-save ─────────────────────────────────────────────────────────

    def after_turn(
        self,
        cost_delta_usd: float,
        input_tokens_delta: int,
        output_tokens_delta: int,
    ) -> None:
        """Call after every completed turn. Updates metadata and flushes to disk.

        This is the primary auto-save path. Called from on_intent() in __main__.py
        after save_turn() completes.
        """
        if self._metadata is None:
            return
        meta = self._metadata
        meta.updated_at = time.time()
        meta.turn_count += 1
        meta.total_cost_usd += cost_delta_usd
        meta.total_input_tokens += input_tokens_delta
        meta.total_output_tokens += output_tokens_delta
        self._write_session_json(meta)
        self._conv_store.upsert_session_metadata(meta)
        self._update_global_index(meta)
        self._update_project_index(meta)

    def save_memory_snapshot(self, snapshot: object) -> None:
        """Write ShortTermMemory snapshot to memory_snapshot.json (atomic)."""
        tmp = self.memory_snapshot_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, default=str, indent=2))
        os.replace(str(tmp), str(self.memory_snapshot_path))

    def load_memory_snapshot(self) -> object | None:
        """Load ShortTermMemory snapshot. Returns None if missing/corrupt."""
        p = self.memory_snapshot_path
        if not p.exists():
            # Fall back to ConversationStore
            return self._conv_store.load_memory_snapshot(self._session_id)
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    # ── clean close ───────────────────────────────────────────────────────

    def close(self) -> None:
        """Mark session as cleanly closed. Call from the TUI finally block.

        Sets incomplete=False, writes final session.json, updates indexes.
        Idempotent.
        """
        if self._closed or self._metadata is None:
            return
        self._closed = True
        meta = self._metadata
        meta.incomplete = False
        meta.incomplete_at = None
        meta.updated_at = time.time()
        self._write_session_json(meta)
        self._conv_store.upsert_session_metadata(meta)
        self._update_global_index(meta)
        self._update_project_index(meta)

    # ── crash recovery helpers ────────────────────────────────────────────

    def find_incomplete_sessions_for_cwd(self) -> list["SessionIndexEntry"]:
        """Return incomplete sessions for the current cwd (candidates for recovery)."""
        return self._conv_store.list_sessions_for_cwd(
            cwd=self._cwd, incomplete_only=True
        )

    # ── listing ───────────────────────────────────────────────────────────

    @classmethod
    def list_recent(
        cls,
        conv_store: "ConversationStore",
        limit: int = 50,
    ) -> list["SessionIndexEntry"]:
        """Return the most recent sessions across all cwds."""
        return conv_store.list_sessions_recent(limit=limit)

    @classmethod
    def list_for_cwd(
        cls,
        conv_store: "ConversationStore",
        cwd: str,
    ) -> list["SessionIndexEntry"]:
        """Return all sessions for a given cwd, most-recent first."""
        return conv_store.list_sessions_for_cwd(cwd=cwd)

    # ── GC ────────────────────────────────────────────────────────────────

    def maybe_gc(self) -> int:
        """Delete oldest sessions if total count > MAX_SESSIONS. Returns deleted count."""
        index = self._load_global_index()
        sessions = index.get("sessions", {})
        if len(sessions) <= MAX_SESSIONS:
            return 0
        # Sort by updated_at ascending (oldest first)
        by_age = sorted(
            sessions.items(), key=lambda kv: kv[1].get("updated_at", 0)
        )
        n_delete = len(sessions) - (MAX_SESSIONS - 50)
        deleted = 0
        for sid, entry in by_age[:n_delete]:
            # Exempt sessions with non-empty tags
            if entry.get("tags") or entry.get("incomplete"):
                continue
            self._delete_session(sid, entry)
            deleted += 1
        return deleted

    # ── private helpers ───────────────────────────────────────────────────

    def _write_session_json(self, meta: "SessionMetadata") -> None:
        tmp = self.session_json_path.with_suffix(".json.tmp")
        self._session_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(meta.to_dict(), indent=2, default=str))
        os.replace(str(tmp), str(self.session_json_path))

    def _load_global_index(self) -> dict:
        if self._global_index_path.exists():
            try:
                return json.loads(self._global_index_path.read_text())
            except Exception:
                return {"version": 1, "sessions": {}}
        return {"version": 1, "sessions": {}}

    def _update_global_index(self, meta: "SessionMetadata") -> None:
        from agenthicc.session.models import SessionIndexEntry  # noqa: PLC0415

        entry = SessionIndexEntry.from_metadata(meta)
        index = self._load_global_index()
        index.setdefault("version", 1)
        index.setdefault("sessions", {})[self._session_id] = {
            "session_id": entry.session_id,
            "cwd": entry.cwd,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "turn_count": entry.turn_count,
            "model_id": entry.model_id,
            "total_cost_usd": entry.total_cost_usd,
            "tags": entry.tags,
            "incomplete": entry.incomplete,
        }
        self._global_index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._global_index_path, index)

    def _update_project_index(self, meta: "SessionMetadata") -> None:
        """Update .agenthicc/sessions.json for backward compat with __main__.py helpers."""
        project_index_path = Path(self._cwd) / ".agenthicc" / "sessions.json"
        try:
            idx: dict = {}
            if project_index_path.exists():
                idx = json.loads(project_index_path.read_text())
            idx[self._session_id] = {
                "cwd": meta.cwd,
                "created_at": meta.created_at,
                "last_used": meta.updated_at,
                "log_path": str(self.events_log_path),
            }
            _atomic_write_json(project_index_path, idx)
        except Exception:
            pass  # project index update failure is non-fatal

    def _delete_session(self, session_id: str, entry: dict) -> None:
        """Remove all traces of a session. Called by GC."""
        # Delete from ConversationStore
        self._conv_store.delete_session(session_id)
        # Delete from global index
        index = self._load_global_index()
        index.get("sessions", {}).pop(session_id, None)
        _atomic_write_json(self._global_index_path, index)
        # Delete session directory (best-effort)
        session_dir = Path(entry.get("cwd", "")) / ".agenthicc" / "sessions" / session_id
        if session_dir.exists():
            import shutil  # noqa: PLC0415
            shutil.rmtree(session_dir, ignore_errors=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via rename."""
    import os  # noqa: PLC0415
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))
```

### 3.2 Creation

New session creation sequence (replaces the ad-hoc code in `_run_tui_session()`):

```
1. generate session_id = uuid.uuid4().hex
2. SessionManager.create()
   a. mkdir ~/.agenthicc/sessions/<id>/   (exist_ok=True)
   b. write session.json with incomplete=True
   c. ConversationStore.upsert_session_metadata(meta)
   d. update ~/.agenthicc/session-index.json
   e. update .agenthicc/sessions.json (backward compat)
3. EventProcessor is initialized with
       event_log_path = ".agenthicc/sessions/<id>/events.jsonl"
4. register SIGTERM and SIGHUP handlers (see 3.2.1)
5. maybe_gc() — clean up if over MAX_SESSIONS
```

The `session_id` used in `EventProcessor` MUST match the session_id written to
`session.json` and both index files. There must be exactly one source of truth.

#### 3.2.1 Signal handlers

Installed immediately after `SessionManager.create()` returns:

```python
import signal

def _install_signal_handlers(session_mgr: SessionManager, conv_store: ConversationStore) -> None:
    """Install SIGTERM and SIGHUP handlers for graceful shutdown."""
    import asyncio  # noqa: PLC0415

    loop = asyncio.get_event_loop()

    def _on_sigterm() -> None:
        loop.create_task(_graceful_shutdown(session_mgr, conv_store, exit_code=0))

    def _on_sighup() -> None:
        loop.create_task(_graceful_shutdown(session_mgr, conv_store, exit_code=0))

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    loop.add_signal_handler(signal.SIGHUP, _on_sighup)


async def _graceful_shutdown(
    session_mgr: SessionManager,
    conv_store: ConversationStore,
    exit_code: int = 0,
) -> None:
    """Flush session state and exit cleanly."""
    session_mgr.close()
    conv_store.close()
    # Give the event loop one tick to flush any in-flight writes
    import asyncio  # noqa: PLC0415
    await asyncio.sleep(0)
    import sys  # noqa: PLC0415
    sys.exit(exit_code)
```

### 3.3 Auto-Save

Auto-save occurs at two points:

1. **After every turn** (`on_intent()` callback in `_run_tui_session()`):
   After `conv_store.save_turn()` completes, call:
   ```python
   session_mgr.after_turn(
       cost_delta_usd=renderer._status.session_cost_usd_delta,
       input_tokens_delta=renderer._status.input_tokens_delta,
       output_tokens_delta=renderer._status.output_tokens_delta,
   )
   session_mgr.save_memory_snapshot(_session_memory.snapshot())
   ```

2. **On SIGTERM / SIGHUP** (via signal handlers installed at creation).

`events.jsonl` is written incrementally by `EventProcessor` on every event emit
(existing behaviour). It does NOT need to be saved explicitly — it is already
append-only and durable.

`memory_snapshot.json` is written after every turn completion (not after every
streaming token). The write is atomic (rename) so it is safe under concurrent
reads from a resumed session in another terminal.

There is no background checkpoint task. The auto-save is synchronous and happens
inline in the `on_intent()` callback, which runs in the asyncio event loop. The
write is fast (< 5 ms for typical snapshot sizes) and does not block the loop.

### 3.4 Resume Protocol

#### --resume {session_id}

```
1. Look up session_id in .agenthicc/sessions.json (existing __main__.py helper)
2. If not found, look up in ~/.agenthicc/session-index.json
3. If still not found, print error and exit
4. SessionManager.resume(session_id)
   a. Load session.json → SessionMetadata
   b. Set incomplete=True (we are running it again)
   c. Write session.json, update indexes
5. EventProcessor.restore_from_log(events.jsonl path)
   - Skips malformed JSON lines with warning
   - If events.jsonl.gz exists and events.jsonl does not, decompress first
6. ConversationStore → load_turns(session_id, limit=20)
7. Print replay block to stdout (committed transcript, never alternate screen):
   a. "── resumed session {id[:12]} ──" separator line
   b. For each of the last 20 Q+A turns:
      - "❯ {user message}" (dim)
      - "● assistant ({model_short})  HH:MM:SS" (agent header color)
      - Markdown-rendered assistant text (via Rich Console.print(Markdown(...)))
   c. "── end of history ──" separator line
8. Restore ShortTermMemory:
   a. Load session_mgr.memory_snapshot_path first
   b. Fall back to ConversationStore.load_memory_snapshot(session_id)
   c. Call _session_memory.restore(snapshot)
9. Continue with live session
```

**What is replayed to the terminal**: The last 20 Q+A pairs are printed to stdout
as Rich Markdown. This is committed transcript — it scrolls permanently into
scrollback. The number 20 is configurable via `memory.resume_replay_turns = 20`
in `agenthicc.toml`.

**ShortTermMemory restoration**: The memory snapshot contains the full conversation
history that the LLM needs for context. It is restored synchronously before the
first new turn begins. If the snapshot is missing or corrupt, the agent starts
fresh (no error — just no prior context).

#### --continue (most recent session for cwd)

```
1. _find_latest_session_for_cwd() → session_id (existing __main__.py helper)
2. If None: print "No previous session found." and start fresh
3. Else: proceed as --resume {session_id}
```

### 3.5 Session Listing

#### /history command (in-TUI)

The `/history` command is handled by the existing command dispatcher. It now calls:

```python
def _cmd_history(args: list[str], conv_store: ConversationStore, cwd: str) -> list[str]:
    """Return formatted lines for /history output (committed to transcript)."""
    sessions = SessionManager.list_for_cwd(conv_store, cwd)[:10]
    lines = ["── Session history ─────────────────────────────────────"]
    for s in sessions:
        dt = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.updated_at))
        cost = f"${s.total_cost_usd:.4f}"
        tags = f"  [{', '.join(s.tags)}]" if s.tags else ""
        incomplete_marker = "  [incomplete]" if s.incomplete else ""
        lines.append(
            f"  {s.session_id[:12]}  {dt}  {s.turn_count} turns  {cost}{tags}{incomplete_marker}"
        )
        lines.append(
            f"    Resume: agenthicc --resume {s.session_id[:12]}"
        )
    lines.append("──────────────────────────────────────────────────────")
    return lines
```

#### `agenthicc sessions` CLI

The existing `_do_sessions()` in `__main__.py` is replaced with a richer output:

```
  SESSION ID    CREATED              UPDATED              TURNS  COST      CWD
  ──────────────────────────────────────────────────────────────────────────────
  abc123456789  2026-06-13 09:41     2026-06-13 15:22  *  23    $0.0452   /home/user/myproject
  def987654321  2026-06-12 18:22     2026-06-12 19:05     87    $2.1400   /home/user/other
  ghi112233445  2026-06-11 14:05     2026-06-11 14:05     3     $0.0031   /home/user/myproject
  [!] jkl556677  2026-06-10 09:00     2026-06-10 09:00     5     $0.0120   /home/user/myproject  [incomplete]

  * = most recent session for current directory
  [!] = incomplete session (crashed or killed)
  Resume with: agenthicc --resume <SESSION ID>
```

---

## 4. Crash Recovery

### 4.1 Detection

A session is considered incomplete if its `SessionMetadata.incomplete == True` at
the time it is read. The `incomplete` flag is set to `True` at two points:

1. **At session creation** (`SessionManager.create()`) — immediately, before any
   events are processed.
2. **At session resume** (`SessionManager.resume()`) — immediately when loading a
   session for resume.

The flag is cleared to `False` ONLY in `SessionManager.close()`, which is called
from the TUI `finally` block. If the process is killed, the `finally` block does
not run and the flag remains `True`.

At startup, `_run_tui_session()` checks for incomplete sessions for the current cwd
before creating a new session:

```python
async def _run_tui_session(...) -> None:
    ...
    conv_store = ConversationStore()
    tmp_mgr = SessionManager(
        session_id="", cwd=os.getcwd(), model_id="", conv_store=conv_store
    )
    incomplete = tmp_mgr.find_incomplete_sessions_for_cwd()
    if incomplete and not resume_id:
        # Offer to resume most-recent incomplete session
        most_recent = max(incomplete, key=lambda s: s.updated_at)
        resume_id = await _offer_crash_recovery(most_recent)
    ...
```

### 4.2 Recovery Protocol

`_offer_crash_recovery()` is called when an incomplete session is detected. It
prints to stdout (committed transcript — no alternate screen):

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Incomplete session detected: abc123456789  (2026-06-12 09:41)           │
│  /home/user/myproject  ·  5 turns  ·  $0.0031                            │
│                                                                          │
│  Resume? [y] Yes   [n] Start fresh   [d] Delete and start fresh          │
└──────────────────────────────────────────────────────────────────────────┘
```

The prompt is rendered as plain stdout lines (not a bottom block). It uses the
same input handling as the approval gate: the user types a single character.

Response handling:

| Key | Action |
|-----|--------|
| `y` | Set `resume_id = most_recent.session_id`; proceed with resume protocol |
| `n` | Start a new session; leave the incomplete session in storage |
| `d` | Delete the incomplete session (ConversationStore + directory); start fresh |
| Any other | Treat as `n` |
| Ctrl+C | Exit the process (SIGINT handling) |

**Partial turn handling on crash recovery**:

If the agent was mid-turn when the crash occurred, the `events.jsonl` may contain
`IntentCreated` without a matching `IntentStatusChanged(status="complete")`. The
`restore_from_log()` replay handles this correctly: the incomplete intent is
restored to `status="pending"`. The agent does NOT automatically retry the partial
turn. The user sees the partial turn history in the resumed transcript and can
re-submit if desired.

**Memory state on crash recovery**:

The `memory_snapshot.json` is written after each completed turn. If the crash
occurred mid-turn, the last snapshot reflects the state before that turn started.
The LLM will not have the partial turn in its context, which is correct — it
cannot reason about a turn that was not completed.

---

## 5. Import/Export

### 5.1 Export Format

Export is triggered by `agenthicc export <session_id> [--output <file>]`. The
output is a single JSON file.

```python
@dataclass
class SessionExport:
    """JSON schema for a full session export."""
    
    format_version: str            # "agenthicc-session-v1"
    exported_at: float             # Unix timestamp
    session: SessionMetadata       # full metadata
    turns: list[TurnRecord]        # all turns in order
    # memory_snapshot intentionally EXCLUDED:
    # it contains message content that may include API keys or secrets
    # injected via @mention. Users can --include-memory to override.
```

**What is excluded by default:**

- `memory_snapshot` (may contain injected file content with secrets)
- `events.jsonl` content (too large; use `--include-events` to add as base64-gz)
- API keys, tokens, or environment variables (never stored in session data)

**What is always included:**

- All `TurnRecord` rows (user and assistant content)
- `ToolCallRecord` entries for each turn (tool name, args, result summary, diff)
- `SessionMetadata` (cwd, model, cost, tokens, tags)

Export JSON schema:

```json
{
  "format_version": "agenthicc-session-v1",
  "exported_at": 1749816000.0,
  "session": {
    "session_id": "abc123...",
    "created_at": 1749812400.0,
    "updated_at": 1749816000.0,
    "turn_count": 23,
    "model_id": "anthropic/claude-sonnet-4-6",
    "cwd": "/home/user/myproject",
    "total_cost_usd": 0.045231,
    "total_input_tokens": 12400,
    "total_output_tokens": 8900,
    "tags": ["incident"],
    "incomplete": false,
    "incomplete_at": null
  },
  "turns": [
    {
      "turn_id": 0,
      "session_id": "abc123...",
      "role": "user",
      "content": "Fix the auth token expiry bug",
      "timestamp": 1749812420.0,
      "model_short": null,
      "tool_calls": [],
      "cost_usd": 0.0,
      "input_tokens": 0,
      "output_tokens": 0
    },
    {
      "turn_id": 1,
      "session_id": "abc123...",
      "role": "assistant",
      "content": "I'll fix the token expiry issue...",
      "timestamp": 1749812430.0,
      "model_short": "claude-sonnet-4-6",
      "tool_calls": [
        {
          "tool_use_id": "toolu_01abc...",
          "tool_name": "read_file",
          "args": {"path": "src/auth/session.py"},
          "state": "success",
          "result_summary": "142 lines",
          "duration_ms": 12.4,
          "error_message": "",
          "diff": ""
        },
        {
          "tool_use_id": "toolu_02def...",
          "tool_name": "write_file",
          "args": {"path": "src/auth/session.py"},
          "state": "success",
          "result_summary": "written",
          "duration_ms": 8.2,
          "error_message": "",
          "diff": "@@ -147,2 +147,2 @@\n-    expiry = datetime.now()\n+    expiry = datetime.now(timezone.utc)"
        }
      ],
      "cost_usd": 0.00234,
      "input_tokens": 1240,
      "output_tokens": 890
    }
  ]
}
```

### 5.2 Import

Import is triggered by `agenthicc import <file>`.

**Validation:**

```python
def _validate_export(data: dict) -> list[str]:
    """Return a list of validation errors. Empty list = valid."""
    errors = []
    if data.get("format_version") != "agenthicc-session-v1":
        errors.append(f"Unknown format_version: {data.get('format_version')!r}")
    if "session" not in data:
        errors.append("Missing 'session' key")
    if "turns" not in data:
        errors.append("Missing 'turns' key")
    session = data.get("session", {})
    if not isinstance(session.get("session_id"), str):
        errors.append("session.session_id must be a string")
    if not isinstance(data.get("turns"), list):
        errors.append("'turns' must be a list")
    return errors
```

**Conflict resolution:**

If the `session_id` in the import file already exists in the `ConversationStore`,
the import creates a NEW session with a fresh `session_id` (UUID4 hex) and all
other fields copied from the import. The old session is not modified.

This is "import as copy" semantics. The user is informed:

```
Imported session as <new_session_id> (original: <original_session_id>)
```

**Import does NOT:**

- Create the `.agenthicc/sessions/<id>/` directory
- Write `events.jsonl` (there are no events in the export)
- Restore `memory_snapshot.json` (excluded from export by default)

After import, the session can be listed with `agenthicc sessions` but cannot be
resumed (no events log). This is by design — import is for read-only browsing of
historical sessions.

---

## 6. New ConversationStore Methods

The following methods must be added to `ConversationStore` in
`src/agenthicc/conversation_store.py`:

```python
def upsert_session_metadata(self, meta: "SessionMetadata") -> None:
    """Insert or update session metadata row."""
    import json as _json  # noqa: PLC0415
    self._conn.execute(
        UPSERT_SESSION_METADATA,
        (
            meta.session_id, meta.created_at, meta.updated_at, meta.turn_count,
            meta.model_id, meta.cwd, meta.total_cost_usd,
            meta.total_input_tokens, meta.total_output_tokens,
            _json.dumps(meta.tags), int(meta.incomplete), meta.incomplete_at,
        ),
    )
    self._conn.commit()

def load_session_metadata(self, session_id: str) -> "SessionMetadata | None":
    """Load session metadata by session_id. Returns None if not found."""
    from agenthicc.session.models import SessionMetadata  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    row = self._conn.execute(SELECT_SESSION_METADATA, (session_id,)).fetchone()
    if row is None:
        return None
    return SessionMetadata(
        session_id=row[0], created_at=row[1], updated_at=row[2],
        turn_count=row[3], model_id=row[4], cwd=row[5],
        total_cost_usd=row[6], total_input_tokens=row[7],
        total_output_tokens=row[8], tags=_json.loads(row[9]),
        incomplete=bool(row[10]), incomplete_at=row[11],
    )

def list_sessions_recent(self, limit: int = 50) -> list["SessionIndexEntry"]:
    """Return most recent sessions ordered by updated_at DESC."""
    from agenthicc.session.models import SessionIndexEntry  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    rows = self._conn.execute(LIST_SESSIONS_RECENT, (limit,)).fetchall()
    return [
        SessionIndexEntry(
            session_id=r[0], cwd=r[1], updated_at=r[2], turn_count=r[3],
            model_id=r[4], total_cost_usd=r[5],
            tags=_json.loads(r[6] or "[]"),
            incomplete=bool(r[7]),
            created_at=0.0,  # not fetched in this query; use load_session_metadata for full record
        )
        for r in rows
    ]

def list_sessions_for_cwd(
    self,
    cwd: str,
    incomplete_only: bool = False,
) -> list["SessionIndexEntry"]:
    """Return sessions for a cwd, most-recent first."""
    from agenthicc.session.models import SessionIndexEntry  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    if incomplete_only:
        query = LIST_SESSIONS_FOR_CWD + " AND incomplete = 1"
    else:
        query = LIST_SESSIONS_FOR_CWD
    rows = self._conn.execute(query, (cwd,)).fetchall()
    return [
        SessionIndexEntry(
            session_id=r[0], cwd=r[1], updated_at=r[2], turn_count=r[3],
            model_id=r[4], total_cost_usd=r[5],
            tags=_json.loads(r[6] or "[]"),
            incomplete=bool(r[7]),
            created_at=0.0,
        )
        for r in rows
    ]

def delete_session(self, session_id: str) -> None:
    """Delete all rows for a session (metadata, turns, memory, summaries, tags)."""
    self._conn.execute(DELETE_SESSION_METADATA, (session_id,))
    self._conn.execute(DELETE_SESSION_TURNS, (session_id,))
    self._conn.execute(DELETE_SESSION_MEMORY, (session_id,))
    self._conn.execute(DELETE_SESSION_SUMMARIES, (session_id,))
    self._conn.execute(DELETE_SESSION_TAGS, (session_id,))
    self._conn.commit()

def save_turn_extended(
    self,
    session_id: str,
    turn_index: int,
    role: str,
    content: str,
    timestamp: float,
    model_short: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    tool_calls: list["ToolCallRecord"] | None = None,
) -> None:
    """Extended version of save_turn() with token counts and tool call metadata."""
    import json as _json, dataclasses  # noqa: PLC0415, E401
    tc_list = [dataclasses.asdict(tc) for tc in (tool_calls or [])]
    self._conn.execute(
        UPSERT_TURN_EXTENDED,
        (session_id, turn_index, role, content, timestamp, model_short,
         input_tokens, output_tokens, cost_usd, _json.dumps(tc_list)),
    )
    self._conn.commit()
```

---

## 7. Integration with `__main__.py`

The existing `_run_tui_session()` is modified to use `SessionManager`. The diff
from current to target:

**Currently** (simplified):
```python
session_id = resume_id or uuid.uuid4().hex
_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")
# ... (no session.json, no global index, no incomplete tracking)
if resume_id:
    _touch_session(resume_id)
else:
    _register_session(session_id)
```

**Target** (with SessionManager):
```python
from agenthicc.session.manager import SessionManager  # noqa: PLC0415
from agenthicc.session.models import SessionMetadata  # noqa: PLC0415

conv_store = ConversationStore()
session_id = resume_id or uuid.uuid4().hex
session_mgr = SessionManager(
    session_id=session_id,
    cwd=os.getcwd(),
    model_id=cfg.execution.effective_model(),
    conv_store=conv_store,
)

if resume_id:
    meta = session_mgr.resume(resume_id)
    if meta is None:
        print(f"Session {resume_id!r} not found.", file=sys.stderr)
        return
    log_path = str(session_mgr.events_log_path)
else:
    # Check for incomplete sessions before creating a new one
    incomplete = session_mgr.find_incomplete_sessions_for_cwd()
    if incomplete:
        chosen = await _offer_crash_recovery(incomplete[0])
        if chosen:
            resume_id = chosen
            session_id = chosen
            session_mgr = SessionManager(
                session_id=session_id, cwd=os.getcwd(),
                model_id=..., conv_store=conv_store,
            )
            meta = session_mgr.resume(resume_id)
            log_path = str(session_mgr.events_log_path)
        else:
            meta = session_mgr.create()
            log_path = str(session_mgr.events_log_path)
    else:
        meta = session_mgr.create()
        log_path = str(session_mgr.events_log_path)

_install_signal_handlers(session_mgr, conv_store)

# ... existing EventProcessor, TranscriptModel, etc. setup ...

try:
    await renderer.run(on_intent)
finally:
    proc_task.cancel()
    await asyncio.gather(proc_task, return_exceptions=True)
    session_mgr.close()          # clears incomplete flag
    conv_store.close()
```

The `on_intent()` callback is updated to call `session_mgr.after_turn()`:

```python
async def on_intent(text: str) -> None:
    idx = _turn_index[0]
    cost_before = renderer._status.session_cost_usd
    input_before = renderer._status.input_tokens
    output_before = renderer._status.output_tokens

    conv_store.save_turn_extended(
        session_id, idx, "user", text, time.time()
    )

    turns_before = len(model.turns)
    await _run_agent_turn(...)

    if len(model.turns) > turns_before:
        last = model.turns[-1]
        content = "\n".join(
            ln.replace(_MD_SENTINEL, "") for ln in last.lines
        ).strip()
        ms = last.agent_name.replace("assistant (", "").rstrip(")")
        cost_delta = renderer._status.session_cost_usd - cost_before
        in_delta = renderer._status.input_tokens - input_before
        out_delta = renderer._status.output_tokens - output_before
        tc_records = [
            ToolCallRecord(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                args=tc.args,
                state=tc.state.name.lower(),
                result_summary=tc.result_summary[:200],
                duration_ms=tc.duration_ms,
                error_message=tc.error_message,
                diff=tc.diff or "",
            )
            for tc in last.tool_calls
        ]
        conv_store.save_turn_extended(
            session_id, idx, "assistant", content, time.time(),
            model_short=ms,
            input_tokens=in_delta,
            output_tokens=out_delta,
            cost_usd=cost_delta,
            tool_calls=tc_records,
        )

    conv_store.save_memory_snapshot(session_id, _session_memory.snapshot())
    session_mgr.after_turn(
        cost_delta_usd=renderer._status.session_cost_usd - cost_before,
        input_tokens_delta=renderer._status.input_tokens - input_before,
        output_tokens_delta=renderer._status.output_tokens - output_before,
    )
    session_mgr.save_memory_snapshot(_session_memory.snapshot())
    _turn_index[0] += 1
```

---

## 8. Full Test Specification

### 8.1 Unit Tests

Location: `tests/unit/test_session_persistence.py`

All unit tests are marked `@pytest.mark.unit`. No I/O to real filesystems — use
`tmp_path` pytest fixture for all file operations.

#### SessionMetadata tests (1-10)

| # | Name | Inputs | Expected | Edge case |
|---|------|--------|----------|-----------|
| 1 | `test_metadata_new_generates_uuid` | `SessionMetadata.new(cwd="/tmp", model_id="m")` | `session_id` is 32-char hex string | UUID uniqueness across 1000 calls |
| 2 | `test_metadata_new_sets_incomplete_false` | `SessionMetadata.new(...)` | `incomplete=False` | — |
| 3 | `test_metadata_to_dict_roundtrip` | Any SessionMetadata instance | `from_dict(to_dict(m)) == m` | tags list preserves order |
| 4 | `test_metadata_from_dict_ignores_unknown_keys` | Dict with extra key `"xyz": 99` | No error; xyz not in result | Future-compat |
| 5 | `test_metadata_from_dict_missing_optional_tags` | Dict without `tags` key | `tags == []` | Defaults |
| 6 | `test_metadata_updated_at_after_created_at` | `SessionMetadata.new(...)` + 0.01s sleep | `updated_at >= created_at` | Monotonic |
| 7 | `test_metadata_incomplete_roundtrip` | `incomplete=True, incomplete_at=123.45` | survives to_dict/from_dict | — |
| 8 | `test_metadata_total_cost_float_precision` | `total_cost_usd=0.000001` | Survives JSON roundtrip without precision loss | IEEE 754 double |
| 9 | `test_session_index_entry_from_metadata` | Any SessionMetadata | All fields match | — |
| 10 | `test_session_index_entry_created_at_zero_default` | `list_sessions_recent()` result | `created_at=0.0` (not fetched in query) | Known omission |

#### TurnRecord and ToolCallRecord tests (11-20)

| # | Name | Inputs | Expected | Edge case |
|---|------|--------|----------|-----------|
| 11 | `test_tool_call_record_state_values` | `state="success"`, `"error"`, `"rejected"` | No error on construction | — |
| 12 | `test_tool_call_record_result_summary_max_200` | `result_summary="x" * 500` | Truncated to 200 chars by `save_turn_extended` | Not enforced in dataclass itself |
| 13 | `test_tool_call_record_diff_empty_string` | No diff on non-file tool | `diff=""` | — |
| 14 | `test_tool_call_record_diff_nonempty` | write_file tool | `diff` contains `---/+++/@@ ` lines | — |
| 15 | `test_turn_record_user_role_zero_cost` | `role="user"` | `cost_usd=0.0, input_tokens=0, output_tokens=0` | — |
| 16 | `test_turn_record_tool_calls_empty_for_user` | `role="user"` | `tool_calls=[]` | — |
| 17 | `test_turn_record_assistant_role_with_tools` | 3 tool calls | `len(tool_calls) == 3` | — |
| 18 | `test_turn_record_content_markdown_preserved` | Multi-line markdown content | Content stored verbatim | No escaping |
| 19 | `test_turn_record_model_short_none_for_user` | `role="user"` | `model_short=None` | — |
| 20 | `test_turn_record_model_short_set_for_assistant` | `role="assistant"` | `model_short="claude-sonnet-4-6"` | — |

#### ConversationStore schema and migration tests (21-35)

| # | Name | Inputs | Expected | Edge case |
|---|------|--------|----------|-----------|
| 21 | `test_migration_creates_all_tables` | Fresh db | 6 tables exist: turns, memory_snapshots, session_metadata, turn_summaries, session_tags, (kv) | — |
| 22 | `test_migration_idempotent` | Run `_migrate()` twice | No error on second run | `CREATE TABLE IF NOT EXISTS` |
| 23 | `test_migration_adds_columns_to_existing_turns` | Old db with turns table (no extra columns) | Columns input_tokens, output_tokens, cost_usd, tool_calls_json exist after migrate | ALTER TABLE ADD COLUMN |
| 24 | `test_upsert_session_metadata_insert` | New session | Row exists in session_metadata | — |
| 25 | `test_upsert_session_metadata_update` | Existing session, new turn_count | Row updated, not duplicated | ON CONFLICT DO UPDATE |
| 26 | `test_load_session_metadata_not_found` | Non-existent session_id | Returns `None` | — |
| 27 | `test_load_session_metadata_found` | Existing session | All fields match | — |
| 28 | `test_list_sessions_recent_order` | 3 sessions with different updated_at | Returned in DESC order | — |
| 29 | `test_list_sessions_recent_limit` | 100 sessions, limit=10 | Returns exactly 10 | — |
| 30 | `test_list_sessions_for_cwd_filters` | Sessions for 2 cwds | Only matching cwd returned | — |
| 31 | `test_list_sessions_for_cwd_incomplete_only` | Mix of complete and incomplete | Only incomplete returned | `incomplete_only=True` |
| 32 | `test_delete_session_removes_all_rows` | Session with turns, memory, tags | All rows gone after delete | Foreign key CASCADE |
| 33 | `test_save_turn_extended_upsert` | Same session_id+turn_index+role twice | Second write wins (no duplicate) | `INSERT OR REPLACE` |
| 34 | `test_save_turn_extended_tool_calls_json` | 2 ToolCallRecords | `tool_calls_json` deserialises to list of 2 | JSON roundtrip |
| 35 | `test_conversation_store_wal_mode` | Any ConversationStore | `PRAGMA journal_mode` == `"wal"` | — |

#### SessionManager creation and lifecycle tests (36-50)

| # | Name | Inputs | Expected | Edge case |
|---|------|--------|----------|-----------|
| 36 | `test_session_manager_create_makes_directory` | `tmp_path` | `session_dir` exists after create() | — |
| 37 | `test_session_manager_create_writes_session_json` | `tmp_path` | `session.json` parseable as SessionMetadata | — |
| 38 | `test_session_manager_create_marks_incomplete` | `tmp_path` | `meta.incomplete == True` after create() | Safety marker |
| 39 | `test_session_manager_close_clears_incomplete` | After create() + close() | `session.json` has `incomplete=false` | — |
| 40 | `test_session_manager_close_idempotent` | close() twice | No error | — |
| 41 | `test_session_manager_after_turn_increments_count` | 3 calls to after_turn | `meta.turn_count == 3` | — |
| 42 | `test_session_manager_after_turn_accumulates_cost` | 3 calls with cost_delta=0.001 | `meta.total_cost_usd == 0.003` | Float arithmetic |
| 43 | `test_session_manager_save_memory_snapshot_atomic` | Normal write | `memory_snapshot.json` exists; no .tmp file left | Rename atomicity |
| 44 | `test_session_manager_load_memory_snapshot_missing` | No snapshot file | Returns `None` (fallback to ConversationStore) | — |
| 45 | `test_session_manager_resume_loads_session_json` | Existing session dir | SessionMetadata populated correctly | — |
| 46 | `test_session_manager_resume_missing_session` | Non-existent session_id | Returns `None` | — |
| 47 | `test_session_manager_resume_marks_incomplete` | Resume existing complete session | `incomplete=True` during run | Re-marks for safety |
| 48 | `test_session_manager_update_global_index_on_create` | `tmp_path` for global index | Entry appears in `session-index.json` | — |
| 49 | `test_session_manager_update_project_index_on_create` | `tmp_path` for project dir | Entry appears in `.agenthicc/sessions.json` | Backward compat |
| 50 | `test_session_manager_gc_deletes_oldest_untagged` | 60 sessions, limit=50 | Oldest 10 untagged sessions deleted | Tagged sessions exempt |
| 51 | `test_session_manager_gc_exempt_tagged` | 60 sessions, all tagged | No sessions deleted | — |
| 52 | `test_session_manager_gc_incomplete_sessions_exempt` | Incomplete + old sessions | Incomplete sessions not deleted | — |
| 53 | `test_atomic_write_json_no_partial_file` | Write + simulate crash mid-write | Final file is either old or new, never corrupt | — |
| 54 | `test_find_incomplete_sessions_for_cwd` | 2 incomplete + 1 complete | Returns 2 | — |

#### Import/Export tests (55-65)

| # | Name | Inputs | Expected | Edge case |
|---|------|--------|----------|-----------|
| 55 | `test_session_export_excludes_memory_snapshot` | Export with memory | `memory_snapshot` not in output | Privacy |
| 56 | `test_session_export_includes_all_turns` | 5 turns | 5 TurnRecords in export | — |
| 57 | `test_session_export_tool_calls_in_turns` | Turns with tool calls | ToolCallRecords in each turn | — |
| 58 | `test_session_export_json_serialisable` | Any session | `json.dumps(export)` raises no error | — |
| 59 | `test_validate_export_valid` | Well-formed export dict | `[]` (no errors) | — |
| 60 | `test_validate_export_missing_turns` | No `turns` key | One error about missing turns | — |
| 61 | `test_validate_export_wrong_version` | `format_version="v99"` | Error about unknown version | — |
| 62 | `test_import_conflict_creates_new_session_id` | Existing session_id in store | New session_id generated | Conflict resolution |
| 63 | `test_import_populates_turns_table` | 3 turns in export | 3 rows in turns table | — |
| 64 | `test_import_populates_session_metadata` | Full export | session_metadata row exists | — |
| 65 | `test_export_import_roundtrip` | Export then import | All TurnRecords match | JSON roundtrip |

### 8.2 Integration Tests

Location: `tests/integration/test_session_lifecycle.py`

All integration tests are marked `@pytest.mark.integration`. They use real SQLite
(via `tmp_path`) and real file I/O but no LLM calls.

| # | Name | Scenario | Expected DB State | Notes |
|---|------|----------|-------------------|-------|
| 1 | `test_full_new_session_create_and_close` | Create session, save 3 turns, close | `session_metadata.incomplete=0`, `turns` has 6 rows (3 user + 3 assistant), `memory_snapshots` has 1 row | Full happy path |
| 2 | `test_session_resume_restores_metadata` | Create + close session, then resume | `incomplete=1` during resume, cleared on close | Resume marks incomplete |
| 3 | `test_session_resume_restores_last_20_turns` | Session with 25 turns | Replay shows exactly 20 turns | `limit=20` in load_turns |
| 4 | `test_session_resume_restores_memory_snapshot` | Session with 3 turns, specific memory content | Memory snapshot content matches | File-based restore |
| 5 | `test_crash_detected_on_next_start` | Create session without calling close() | `incomplete=1` in session_metadata | Simulated crash |
| 6 | `test_crash_recovery_offer_y` | Incomplete session + user types "y" | Session resumed, incomplete cleared on close | Mock user input |
| 7 | `test_crash_recovery_offer_n` | Incomplete session + user types "n" | New session created, old still incomplete | Old session untouched |
| 8 | `test_crash_recovery_offer_d` | Incomplete session + user types "d" | Old session deleted from DB and disk | Cleanup |
| 9 | `test_session_gc_triggers_at_max` | Create MAX_SESSIONS+1 sessions | Oldest session deleted from DB and index | GC threshold |
| 10 | `test_session_gc_does_not_delete_tagged` | MAX_SESSIONS+1 sessions, oldest has tag | Oldest tagged session survives | Exempt list |
| 11 | `test_global_index_written_on_create` | Create session | `~/.agenthicc/session-index.json` has entry | Global index |
| 12 | `test_global_index_written_on_close` | Create + close | Global index entry has `incomplete=false` | — |
| 13 | `test_project_index_backward_compat` | Create session | `.agenthicc/sessions.json` has entry in existing format | __main__.py compat |
| 14 | `test_multiple_sessions_same_cwd` | 3 sessions in same cwd | `list_sessions_for_cwd` returns all 3 | — |
| 15 | `test_multiple_sessions_different_cwd` | 2 sessions each in 2 cwds | `list_sessions_for_cwd(cwd1)` returns 2, not 4 | — |
| 16 | `test_save_turn_extended_stores_tool_calls` | Assistant turn with 2 tool calls | `tool_calls_json` parses to list of 2 dicts | — |
| 17 | `test_conv_store_migration_on_old_db` | DB created before migration | After connect, new tables exist | Schema upgrade |
| 18 | `test_events_jsonl_rotation_at_100mb` | Write 100+ MB to events.jsonl | events.jsonl rotated to events.jsonl.1 | Log rotation |
| 19 | `test_memory_snapshot_atomic_write` | Save snapshot during concurrent read | Reader never sees partial write | Rename atomicity |
| 20 | `test_session_listing_ordered_by_updated_at` | Create 3 sessions, update middle one last | Middle session first in list | ORDER BY updated_at DESC |

### 8.3 E2E Tests

Location: `tests/e2e/test_session_e2e.py`

All E2E tests are marked `@pytest.mark.e2e`. They simulate the full `_run_tui_session()`
lifecycle using `FakeTerminal` and stubbed LLM responses (no real API calls).

| # | Name | Scenario | Expected Recovery | How Crash is Simulated |
|---|------|----------|-------------------|------------------------|
| 1 | `test_fresh_session_creates_all_files` | Cold start, 1 turn, clean exit | `session.json` exists, `events.jsonl` non-empty, `memory_snapshot.json` exists, `incomplete=False` | Clean `close()` |
| 2 | `test_resume_replays_last_turns_to_stdout` | Session with 5 turns, then resume | Last 20 (all 5) turns printed to FakeTerminal committed lines | Resume flag |
| 3 | `test_resume_with_25_turns_replays_20` | Session with 25 turns, then resume | Exactly 20 turns printed (not 25, not 21) | Resume flag |
| 4 | `test_crash_and_recovery_within_5s` | Crash mid-turn (SIGKILL simulated via `os.kill(os.getpid(), signal.SIGKILL)` in subprocess) | Recovery offer appears within 5 seconds of next start | `subprocess.Popen` with `SIGKILL` |
| 5 | `test_sigterm_calls_close` | Send SIGTERM to running session | `session.json` has `incomplete=False` after process exits | `signal.raise_signal(signal.SIGTERM)` in asyncio task |
| 6 | `test_sighup_calls_close` | Send SIGHUP to running session | `session.json` has `incomplete=False` after process exits | `signal.raise_signal(signal.SIGHUP)` |
| 7 | `test_corrupt_events_jsonl_skips_bad_lines` | Truncate events.jsonl mid-line, then resume | Session resumes with partial state; warning printed to stderr | Write partial JSON line |
| 8 | `test_export_then_import_then_list` | Export session, import to new DB | `agenthicc sessions` lists imported session | Full roundtrip |
| 9 | `test_gc_runs_on_create_at_max` | Create MAX_SESSIONS+1 sessions (mocked) | Total session count <= MAX_SESSIONS after last create | Mock ConversationStore.list_sessions_recent |
| 10 | `test_continue_flag_finds_most_recent` | 2 sessions for same cwd, `--continue` | Most recent session resumed (not oldest) | `updated_at` ordering |

---

## 9. Acceptance Criteria

All criteria are binary (pass/fail). The implementation is complete when all pass.

### 9.1 Data Integrity

| # | Criterion | Measurement |
|---|-----------|-------------|
| AC-1 | Every completed turn is durable before the next LLM call begins | Verify: `fsync` / WAL checkpoint confirms writes in integration test after each `save_turn_extended()` call |
| AC-2 | `session.json` always reflects the last completed turn count | Assert `SessionMetadata.turn_count == len(conv_store.load_turns(session_id))` after N turns |
| AC-3 | `incomplete=True` is always set before any events are written | Assert `session.json.incomplete == True` immediately after `SessionManager.create()` returns, before the first event is emitted |
| AC-4 | `incomplete=False` is set within 5 seconds of process exit on SIGTERM | Measure time from SIGTERM receipt to `incomplete=False` in `session.json` |
| AC-5 | `memory_snapshot.json` is never partially written | Read snapshot during concurrent write (10 threads, 1000 iterations); all reads return valid JSON |
| AC-6 | `session-index.json` is never corrupted by concurrent writes from 2 processes | 2 processes write their own session entries simultaneously; both entries present in final file |
| AC-7 | Events log rotation does not lose any events | After rotation, all events from before rotation are in `events.jsonl.1`; new events in `events.jsonl` |
| AC-8 | All 6 new/extended SQL tables exist after migration on any existing database | Schema check in `test_migration_creates_all_tables` and `test_conv_store_migration_on_old_db` |

### 9.2 Recovery

| # | Criterion | Measurement |
|---|-----------|-------------|
| AC-9 | Crash recovery offer appears within 500ms of next startup when incomplete session exists | Measured from first byte of process output to crash recovery prompt |
| AC-10 | `--resume` restores the last 20 turns to the terminal within 2 seconds | Measured from `_run_tui_session()` entry to last history line printed |
| AC-11 | Corrupt `events.jsonl` (truncated mid-line) is handled gracefully | Process starts with warning to stderr, no traceback, session resumes partially |
| AC-12 | `--continue` finds the most recent session for the current cwd | Assert `session_id == max_updated_at_session.session_id` |
| AC-13 | Crash recovery "Delete" option removes all session artifacts | After "d" choice: no rows in any table, no directory on disk |

### 9.3 Performance

| # | Criterion | Measurement |
|---|-----------|-------------|
| AC-14 | `after_turn()` completes in < 10ms (p99) | `timeit` over 1000 calls with typical session metadata |
| AC-15 | `ConversationStore.list_sessions_recent(limit=50)` completes in < 5ms | `timeit` with 500 sessions in DB |
| AC-16 | Resume of a 200-turn session completes within 2 seconds (including replay) | Wall-clock time from `_run_tui_session()` entry to first new input bar draw |
| AC-17 | `SessionManager.gc()` completes in < 200ms with 600 sessions | `timeit` with 600 rows in session_metadata |

### 9.4 Test Coverage

| # | Criterion | Measurement |
|---|-----------|-------------|
| AC-18 | `src/agenthicc/session/models.py` ≥ 95% line coverage | `pytest --cov=agenthicc.session.models` |
| AC-19 | `src/agenthicc/session/manager.py` ≥ 90% line coverage | `pytest --cov=agenthicc.session.manager` |
| AC-20 | All 65 unit tests pass | `uv run pytest tests/unit/test_session_persistence.py -q` |
| AC-21 | All 20 integration tests pass | `uv run pytest tests/integration/test_session_lifecycle.py -q` |
| AC-22 | All 10 E2E tests pass | `uv run pytest tests/e2e/test_session_e2e.py -q` |
| AC-23 | `uv run mypy src/agenthicc/session/` exits 0 | mypy strict mode on session package |
| AC-24 | `uv run ruff check src/agenthicc/session/` exits 0 | ruff linting on session package |

### 9.5 Backward Compatibility

| # | Criterion | Measurement |
|---|-----------|-------------|
| AC-25 | Existing `agenthicc sessions` CLI output is a superset of current format (no removed columns) | Verify against current `_do_sessions()` output format |
| AC-26 | `--resume <id>` works with sessions created before this PRD (events.jsonl only, no session.json) | Integration test with old-format session directory |
| AC-27 | `ConversationStore` existing `save_turn()` / `load_turns()` methods still work unchanged | All existing `test_config.py` and `test_event_processor.py` tests continue to pass |
| AC-28 | `.agenthicc/sessions.json` per-project index is kept up to date | Assert entry present after `SessionManager.create()` using `_load_session_index()` from `__main__.py` |

---

## 10. New Files Required

| File | Description |
|------|-------------|
| `src/agenthicc/session/__init__.py` | Re-exports `SessionMetadata`, `SessionIndexEntry`, `TurnRecord`, `ToolCallRecord`, `SessionExport`, `SessionManager` |
| `src/agenthicc/session/models.py` | `SessionMetadata`, `SessionIndexEntry`, `TurnRecord`, `ToolCallRecord`, `SessionExport`, `IncompleteMarker` dataclasses |
| `src/agenthicc/session/manager.py` | `SessionManager` class |
| `tests/unit/test_session_persistence.py` | 65 unit tests |
| `tests/integration/test_session_lifecycle.py` | 20 integration tests |
| `tests/e2e/test_session_e2e.py` | 10 E2E tests |

## 11. Modified Files

| File | Change |
|------|--------|
| `src/agenthicc/conversation_store.py` | Add `_migrate()`, `upsert_session_metadata()`, `load_session_metadata()`, `list_sessions_recent()`, `list_sessions_for_cwd()`, `delete_session()`, `save_turn_extended()`. Add new SQL DDL constants. Extend `__init__()` to call `_migrate()`. |
| `src/agenthicc/__main__.py` | Replace ad-hoc session index helpers with `SessionManager`. Update `_run_tui_session()`, `on_intent()`. Add `_install_signal_handlers()`, `_offer_crash_recovery()`, `_do_sessions()` (richer output). |
| `src/agenthicc/tui/transcript.py` | Add `diff: str = ""` field to `ToolCallEntry` dataclass. |

---

*End of document.*
