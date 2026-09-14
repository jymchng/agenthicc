---
name: using-memory
version: 1.0.0
tags: [memory, sqlite, layers, artifacts, router]
description: >-
  Use the three-tier memory model: SessionMemoryLayer for process-local values,
  ProjectMemoryLayer for project SQLite and artifacts, GlobalMemoryLayer for
  user-wide storage, and MemoryRouter for routing reads and writes.
---

# Skill: Using Memory

Agenthicc has three memory tiers behind one router. Agents never touch a layer
directly — `MemoryRouter` is the single dispatch point that resolves the tier,
applies the permission check, and returns a plain dictionary payload.

## When to use this skill

Use this skill when you need to:

- Decide which tier a value belongs in
- Read and write memory from an agent or from Python
- Publish and fetch binary or text artifacts
- Understand TTL, LRU eviction, and namespacing
- Add or replace the permission policy
- Search past agent output semantically

---

## The three tiers

| Tier | Class | Backend | Lifetime | TTL | Eviction |
|---|---|---|---|---|---|
| Session | `SessionMemoryLayer` | In-process `OrderedDict` | One process | Per-entry, on `set(ttl=...)` | LRU at `max_entries` + lazy TTL |
| Project | `ProjectMemoryLayer` | SQLite file | Project | None | Manual `delete` |
| Global | `GlobalMemoryLayer` | SQLite file | Cross-project | None | Manual `delete` |

`MemoryTier` is a string enum (`src/agenthicc/memory/layers.py:41`):

```python
class MemoryTier(str, enum.Enum):
    SESSION = "session"
    PROJECT = "project"
    GLOBAL_ = "global"   # trailing underscore: `global` is a keyword
```

`GlobalMemoryLayer` subclasses `ProjectMemoryLayer` and defaults to
`~/.agenthicc/global.db` (`src/agenthicc/memory/layers.py:345`). Pass `db_path` explicitly in tests.

Choosing a tier:

- **Session** — scratch space, intermediate results. Fastest; lost on exit.
- **Project** — anything that must survive a restart or be shared between agents
  in the same project.
- **Global** — knowledge you want across every project on the machine.

---

## `MemoryRouter`

```python
import asyncio

from agenthicc.memory import (
    GlobalMemoryLayer,
    MemoryRouter,
    ProjectMemoryLayer,
    SessionMemoryLayer,
)

session = SessionMemoryLayer(max_entries=1024)
project = ProjectMemoryLayer(".agenthicc/memory/project.db")
global_layer = GlobalMemoryLayer()          # ~/.agenthicc/global.db

router = MemoryRouter(session, project, global_layer)
```

All three layers are **required, positional** arguments; the fourth parameter is
an optional permission checker. There is no default construction that omits a
tier.

> **There are no key prefixes.** The old `"session:"` / `"project:"` /
> `"global:"` convention does not exist. The tier is an explicit argument.

### `read` and `write` are async and return dictionaries

```python
async def demo(router: MemoryRouter) -> None:
    await router.write("approved_plan", "ship it", tier="project")

    result = await router.read("approved_plan", tier="project")
    if result["found"]:
        print(result["value"])

    # Session-only TTL, in seconds.
    await router.write("scratch", {"step": 1}, tier="session", ttl=3600)
```

| Method | Signature | Returns |
|---|---|---|
| `read` | `(key, tier="session", namespace="default", agent_id=None)` | `{"found": bool, "value": ...}` |
| `write` | `(key, value, tier="session", namespace="default", ttl=None, agent_id=None)` | `{"ok": bool, "key": key}` |

`tier` accepts the string (`"project"`) or the enum. `ttl` applies to the session
tier only and is silently ignored on the persistent tiers.

### Artifacts are content-addressed

```python
async def share_report(router: MemoryRouter) -> None:
    published = await router.publish_artifact(
        b"<html>... coverage report ...</html>",
        content_type="text/html",
        published_by="worker-1",
    )
    artifact_id = published["artifact_id"]      # sha256 hex of the bytes
    print(published["size_bytes"])

    fetched = await router.read_artifact(artifact_id)

    if fetched["found"]:
        with open("report.html", "wb") as f:
            f.write(fetched["content"])
```

| Method | Signature | Returns |
|---|---|---|
| `publish_artifact` | `(content, content_type="text/plain", published_by=None)` | `{"ok": bool, "artifact_id": str, "size_bytes": int}` |
| `read_artifact` | `(artifact_id, agent_id=None)` | `{"found": bool, "content": bytes\|None, "content_type": str\|None}` |

> **Not keyed by `(workflow_id, name)`.** The artifact id *is* the SHA-256 hex
> digest of the raw content, and publishing is **idempotent**: identical bytes
> always yield the same `artifact_id`. To "share a report between agents",
> publish it once and pass the `artifact_id` through conversation or memory.

---

## The layers directly

Reach for the layers only when you are outside a workflow, or writing tests.

### `SessionMemoryLayer`

```python
from agenthicc.memory import SessionMemoryLayer

mem = SessionMemoryLayer(max_entries=512)   # that is the only constructor arg

await mem.set("analysis:doc-1", {"summary": "...", "keywords": ["auth", "jwt"]},
              ttl=1800)                      # ttl is per-write
found, value = mem.get("analysis:doc-1")     # get() is SYNCHRONOUS
await mem.delete("analysis:doc-1")
removed = await mem.prune_expired()          # async
```

Two asymmetries that catch people out:

- `get()` is **synchronous** and lock-free; `set`, `delete`, and `prune_expired`
  are **async**.
- There is no `ttl_seconds` constructor argument. TTL is supplied per write.

TTL expiry is **lazy**: an expired entry is evicted by the `get()` that notices
it, or by an explicit `prune_expired()`. Reads take no lock; writes are
serialised through one `asyncio.Lock`. LRU eviction drops the least-recently-used
entry once `max_entries` is exceeded.

### `ProjectMemoryLayer`

```python
from agenthicc.memory import ProjectMemoryLayer

project = ProjectMemoryLayer(".agenthicc/memory/project.db")

await project.set("task:t001:result", {"status": "complete", "tests_passed": 42})
found, value = await project.get("task:t001:result")   # JSON round-trip

record = await project.put_artifact(b"coverage bytes", content_type="text/html")
same = await project.get_artifact(record.artifact_id)
await project.vacuum()
```

Values are JSON-encoded on write (`default=str`) and JSON-decoded on read, so
anything you store must survive a JSON round trip. All SQLite work runs on a
worker thread via `asyncio.to_thread` with a short-lived connection per call, and
the database uses WAL mode, so readers do not block the single writer.

> **Parent directories are created for you.** The constructor calls
> `Path(db_path).parent.mkdir(parents=True, exist_ok=True)`
> (`src/agenthicc/memory/layers.py:215-218`), so you do not need to pre-create the directory.

---

## Namespaces

Every layer keys on `(namespace, key)`. The default namespace is `"default"`.
Namespaces are how you isolate two agents or two workflows that would otherwise
collide on the same key name:

```python
await router.write("status", "planning", tier="project", namespace="worker-1")
await router.write("status", "reviewing", tier="project", namespace="worker-2")

a = await router.read("status", tier="project", namespace="worker-1")
b = await router.read("status", tier="project", namespace="worker-2")
assert a["value"] != b["value"]
```

---

## Permissions

`PermissionChecker` is a **type alias**, not a class
(`src/agenthicc/memory/router.py:19-22`):

```python
PermissionChecker = Callable[[str | None, MemoryTier, str], bool]   # (agent_id, tier, operation)
```

The default is `allow_all`, which returns `True` unconditionally. Supply your
own callable as the fourth router argument:

```python
from agenthicc.memory import GlobalMemoryLayer, MemoryRouter, MemoryTier

def policy(agent_id: str | None, tier: MemoryTier, operation: str) -> bool:
    if tier is MemoryTier.GLOBAL_ and operation == "write":
        return agent_id == "admin"
    if tier is MemoryTier.PROJECT and agent_id == "untrusted":
        return False
    return True

router = MemoryRouter(session, project, GlobalMemoryLayer(), policy)
```

A denial does **not** raise. The router returns a falsy payload carrying an
error, so it can be surfaced straight back to the model:

```python
{"found": False, "value": None, "error": "permission_denied"}   # read
{"ok": False, "key": "k", "error": "permission_denied"}         # write
```

That is deliberate — an agent tool should report a refusal, not crash the turn.

---

## Semantic search

`SemanticIndex` backs semantic recall over past agent output
(`src/agenthicc/memory/vector.py:101`). It works without a model by falling back
to a bag-of-words/TF-IDF store, and accepts an embedding when one is available.

```python
from agenthicc.memory import SemanticIndex

index = SemanticIndex()
await index.add("doc-42", "the auth module now uses JWT", embedding=None)
hits = await index.search("authentication approach", top_k=5)   # [(doc_id, score), ...]
```

---

## The agent-facing tools

Agents get four tools from `src/agenthicc/workflows/memory_tools.py:133`:
`memory_write`, `memory_read`, `semantic_search`, `publish_artifact`.

| Tool | Arguments | Returns |
|---|---|---|
| `memory_write` | `key, value, scope="project", namespace="default", ttl_seconds=0.0` | `{"ok", "key"}` |
| `memory_read` | `key, scope="project", namespace="default"` | `{"found", "value"}` |
| `semantic_search` | `query, top_k=5` | `{"results": [{"doc_id", "score"}, ...]}` |
| `publish_artifact` | `content, content_type="text/plain"` | `{"ok", "artifact_id", "size_bytes"}` |

Note that the tool surface names the tier `scope`, and `memory_write` names the
TTL `ttl_seconds` (mapping `0` to "no expiry"). The layer API uses `tier` and
`ttl`; keep the two vocabularies straight when you move between them.

When the router or index is unavailable, these tools return
`{"ok": false, "error": "memory_not_available"}` (or a `"results": []` variant)
rather than raising, so callers never need to guard.

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `TypeError: MemoryRouter.__init__() missing 1 required positional argument` | Passed only session + project | Pass all three layers |
| `AttributeError` calling `router.read(...)` without `await` | `read`/`write` are async | `await` them |
| `result["value"]` is `None` on a stored value | Read a different namespace or tier | Check `namespace` and `tier` |
| Ignoring the return value and reading the value directly | `read` returns a dict | Read `result["value"]`, check `result["found"]` |
| `mem.get()` awaited | `get()` is synchronous | Drop the `await` |
| `TypeError: unexpected keyword 'ttl_seconds'` on a layer | Layers use `ttl` | Use `ttl` on the layer, `ttl_seconds` only on the tool |
| `memory_read` returns `permission_denied` | Policy denied the tier/operation | Adjust the checker or the tier |
| Artifact lookup by name returns nothing | Artifacts are keyed by SHA-256 id | Store the `artifact_id`, not a filename |
| Session value gone after a restart | Session tier is in-process | Use the project or global tier |

---

## Key points

- One router, three tiers; the tier is always an explicit argument.
- `read`/`write` are async and return dictionaries — check `found`/`ok` rather
  than assuming success.
- Session `get()` is synchronous; `set`/`delete`/`prune_expired` are async.
- TTL is per-write on the session tier and ignored elsewhere; there is no
  `ttl_seconds` constructor argument.
- Project and global layers create their own parent directories and use WAL mode
  on a worker thread.
- Namespacing is `(namespace, key)`; the default namespace is `"default"`.
- `PermissionChecker` is a callable type alias; denials return an error payload
  instead of raising.
- Artifacts are content-addressed by SHA-256 and publishing is idempotent.
- `SemanticIndex` works without an embedding model.
