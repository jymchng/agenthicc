# Memory

agenthicc provides three memory tiers with distinct durability and scope
characteristics.  All agent access is mediated through `MemoryRouter`, which
enforces permission checks and routes to the correct backing layer.

---

## Three-tier comparison

| | Session | Project | Global |
|---|---|---|---|
| **Backing store** | In-process LRU dict | SQLite (WAL mode) | SQLite (WAL mode) |
| **Default location** | Process memory | `.agenthicc/project.db` | `~/.agenthicc/global.db` |
| **Survives process exit** | No | Yes | Yes |
| **TTL support** | Yes (per-entry) | No | No |
| **LRU eviction** | Yes (max_entries) | No | No |
| **Namespaces** | Yes | Yes | Yes |
| **Artifact storage** | No | Yes | Yes |
| **Write lock** | asyncio.Lock | asyncio.Lock | asyncio.Lock |
| **Read blocking** | Never | Never (to_thread) | Never (to_thread) |
| **Tier constant** | `MemoryTier.SESSION` | `MemoryTier.PROJECT` | `MemoryTier.GLOBAL_` |

---

## Session memory

`SessionMemoryLayer` is an `OrderedDict`-backed LRU cache with optional
per-entry TTL.  It lives in process memory and is lost when the process exits.
Use it for:

- Intermediate agent computation results within a single run.
- Rate-limit counters or deduplication sets.
- Short-lived scratch space with automatic expiry.

### Constructor

```python
from agenthicc.memory.layers import SessionMemoryLayer

# Default capacity: 1024 entries, no global TTL
session = SessionMemoryLayer(max_entries=512)
```

### Reading and writing

```python
# Write with a 60-second TTL
await session.set("last_fetch", {"url": "...", "status": 200}, ttl=60.0)

# Read — returns (found: bool, value: Any)
found, value = session.get("last_fetch")
if found:
    print(value["status"])

# Delete
await session.delete("last_fetch")

# Evict all expired entries (call from a periodic background task)
removed = await session.prune_expired()
```

### LRU eviction

When the cache reaches `max_entries`, the least-recently-used entry is
evicted automatically on each `set()` call.  Accessing an entry (via `get()`)
promotes it to most-recently-used, preventing premature eviction of hot keys.

---

## Project memory

`ProjectMemoryLayer` persists key-value data and content-addressed artifacts
to a SQLite database scoped to the project directory.  It survives process
restarts and is shared by all agents in the same project.

SQLite WAL mode is enabled by default, allowing concurrent readers while a
single serialised writer holds the write lock.  All disk I/O runs on a worker
thread via `asyncio.to_thread` so the event loop is never blocked.

### Constructor

```python
from agenthicc.memory.layers import ProjectMemoryLayer

project = ProjectMemoryLayer(db_path=".agenthicc/project.db")
```

The `db_path` parent directory is created automatically.

### Key-value operations

```python
# Write
await project.set("agent_plan", {"steps": [...]}, namespace="planner-agent")

# Read — async, returns (found, value)
found, value = await project.get("agent_plan", namespace="planner-agent")

# Delete
await project.delete("agent_plan", namespace="planner-agent")
```

**Namespaces** isolate agents from each other within the same database.
Use `agent_id` as the namespace to guarantee per-agent isolation.

### Artifact storage

Artifacts are content-addressed by the SHA-256 of their raw bytes.
Publishing the same content twice is idempotent — it returns the same
`artifact_id` both times.

```python
from agenthicc.memory.layers import ArtifactRecord

# Publish (str or bytes)
record: ArtifactRecord = await project.put_artifact(
    content="# Migration plan\n\n...",
    content_type="text/markdown",
    published_by="planner-agent",
)
print(record.artifact_id)   # sha256 hex
print(record.size_bytes)

# Retrieve by id
record = await project.get_artifact(record.artifact_id)
if record is not None:
    text = record.content.decode("utf-8")
```

---

## Global memory

`GlobalMemoryLayer` extends `ProjectMemoryLayer` with a default path of
`~/.agenthicc/global.db`.  Use it for data that should persist across
different projects — e.g. user preferences, cached model credentials, or
global tool registrations.

```python
from agenthicc.memory.layers import GlobalMemoryLayer

# Uses ~/.agenthicc/global.db by default
global_mem = GlobalMemoryLayer()

# Explicit path (useful in tests)
global_mem = GlobalMemoryLayer(db_path="/tmp/test-global.db")
```

The API is identical to `ProjectMemoryLayer`.

---

## MemoryRouter

`MemoryRouter` is the single dispatch point for all agent memory access.
Agents never call the backing layers directly in production code.

### Construction

```python
from agenthicc.memory.layers import (
    SessionMemoryLayer,
    ProjectMemoryLayer,
    GlobalMemoryLayer,
)
from agenthicc.memory.router import MemoryRouter, allow_all

router = MemoryRouter(
    session_layer=SessionMemoryLayer(max_entries=1024),
    project_layer=ProjectMemoryLayer(".agenthicc/project.db"),
    global_layer=GlobalMemoryLayer(),
    permission_checker=allow_all,   # default; see below
)
```

### Key-value via router

```python
# Write to session tier (default)
result = await router.write("scratch", {"x": 1}, tier="session", agent_id="agent-01")
# {"ok": True, "key": "scratch"}

# Write to project tier with namespace
result = await router.write(
    "plan", plan_data,
    tier="project",
    namespace="planner",
    agent_id="agent-01",
)

# Read from project tier
result = await router.read("plan", tier="project", namespace="planner", agent_id="agent-01")
# {"found": True, "value": {...}}

# Session tier with TTL
result = await router.write("token", "abc123", tier="session", ttl=300.0, agent_id="agent-01")
```

Tiers can also be passed as `MemoryTier` enum values:

```python
from agenthicc.memory.layers import MemoryTier
await router.read("key", tier=MemoryTier.PROJECT, agent_id="agent-01")
```

### Artifacts via router

```python
# Publish (always goes to project layer)
result = await router.publish_artifact(
    content=b"\x89PNG...",
    content_type="image/png",
    published_by="screenshot-agent",
)
# {"ok": True, "artifact_id": "<sha256>", "size_bytes": 12345}

# Retrieve
result = await router.read_artifact(artifact_id, agent_id="consumer-agent")
# {"found": True, "content": b"...", "content_type": "image/png"}
```

---

## Permission checker pattern

`PermissionChecker` is a callable with signature:

```python
(agent_id: str | None, tier: MemoryTier, operation: str) -> bool
```

`operation` is either `"read"` or `"write"`.  Denied operations return a
result dict with `{"ok": False, "error": "permission_denied"}` rather than
raising, so the result can be surfaced directly as a tool payload without
special error handling.

### Example: read-only global tier

```python
from agenthicc.memory.layers import MemoryTier
from agenthicc.memory.router import PermissionChecker

def restricted_checker(
    agent_id: str | None,
    tier: MemoryTier,
    operation: str,
) -> bool:
    # Untrusted agents cannot write to the global tier
    if tier is MemoryTier.GLOBAL_ and operation == "write":
        trusted = {"orchestrator", "admin"}
        return agent_id in trusted
    return True

router = MemoryRouter(
    session_layer=SessionMemoryLayer(),
    project_layer=ProjectMemoryLayer(".agenthicc/project.db"),
    global_layer=GlobalMemoryLayer(),
    permission_checker=restricted_checker,
)
```

---

## Next steps

- [Writing agents](agents.md) — use memory tools from within agents
- [Lifecycle hooks](hooks.md) — hook into memory write events for auditing
- [Kernel reference](../reference/kernel.md) — `AppState` fields for tool registrations
