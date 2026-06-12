---
skill: using-memory
version: 1.0.0
tags: [memory, sqlite, lru, artifacts, ttl, three-tier]
summary: Guide to the three-tier memory architecture — session LRU/TTL, project SQLite, global SQLite, artifact sharing, and MemoryRouter.
---

# Skill: Using Memory

## When to use this skill

Use this skill when you need to:
- Store and retrieve session, project, or global state from agents
- Share binary artifacts (files, reports) between agents in the same workflow
- Understand TTL expiry and LRU eviction in the session layer
- Route memory reads and writes through the `MemoryRouter` by key prefix
- Check permissions before reading sensitive memory entries

---

## Tier comparison

| Tier | Class | Backend | Scope | TTL | Eviction |
|---|---|---|---|---|---|
| Session | `SessionMemoryLayer` | In-process dict | Single run | `ttl_seconds` (default 86400) | LRU (`maxsize`) + TTL |
| Project | `ProjectMemoryLayer` | SQLite file | Project lifetime | None | Manual `delete` |
| Global | `GlobalMemoryLayer` | SQLite file | Cross-project | None | Manual `delete` |

**When to use each tier:**

- **Session**: scratch space, intermediate computation results, caches that
  expire at end of run. Fastest — pure in-process Python.
- **Project**: agent memory that should survive a crash and restart; task results
  that other agents in the same project need to read; artifacts produced by a
  workflow.
- **Global**: shared knowledge base, pre-computed embeddings, org-wide settings
  that span multiple projects.

---

## MemoryRouter

`MemoryRouter` dispatches by key prefix so agents don't need to know which tier
to use:

| Key prefix | Routed to |
|---|---|
| `session:` | `SessionMemoryLayer` |
| `project:` | `ProjectMemoryLayer` |
| `global:` | `GlobalMemoryLayer` (falls back to project if absent) |

```python
from agenthicc.memory.router import MemoryRouter
from agenthicc.memory.layers import SessionMemoryLayer, ProjectMemoryLayer

session = SessionMemoryLayer(maxsize=256, ttl_seconds=3600)
project = ProjectMemoryLayer(db_path=".agenthicc/memory/project.db")
router = MemoryRouter(session=session, project=project)
```

### read

```python
value = router.read("session:scratchpad/task-001")
# None if key is absent or expired
```

### write

```python
router.write("session:scratchpad/task-001", {"analysis": "...", "score": 0.92})
router.write("project:results/task-001", {"status": "complete", "output": "..."})
router.write("global:embeddings/doc-42", embedding_vector)
```

### publish_artifact / read_artifact

```python
# Publish a binary artifact from a workflow
router.publish_artifact(
    workflow_id="wf-abc123",
    name="test_report.html",
    data=report_bytes,
)

# Read it back from any agent in the same workflow
data = router.read_artifact(
    workflow_id="wf-abc123",
    name="test_report.html",
)
if data is not None:
    with open("report.html", "wb") as f:
        f.write(data)
```

---

## SessionMemoryLayer direct usage

```python
from agenthicc.memory.layers import SessionMemoryLayer

mem = SessionMemoryLayer(maxsize=512, ttl_seconds=1800)

# Write with TTL
mem.set("analysis:doc-1", {"summary": "...", "keywords": ["auth", "jwt"]})

# Read (returns None if expired or absent)
result = mem.get("analysis:doc-1")

# Delete explicitly
mem.delete("analysis:doc-1")

# Prune all expired entries and get count
removed = mem.prune_expired()
print(f"Pruned {removed} expired entries")
```

---

## ProjectMemoryLayer direct usage

```python
from agenthicc.memory.layers import ProjectMemoryLayer

project_mem = ProjectMemoryLayer(db_path=".agenthicc/memory/project.db")

# Store a task result persistently
project_mem.set("task:t001:result", {
    "status": "complete",
    "files_modified": 4,
    "tests_passed": 42,
})

# Read it back (survives process restart)
result = project_mem.get("task:t001:result")

# Publish a binary artifact
with open("coverage_report.html", "rb") as f:
    project_mem.publish_artifact("wf-abc123", "coverage_report.html", f.read())

# Retrieve it
data = project_mem.get_artifact("wf-abc123", "coverage_report.html")
```

---

## Permission checking

Before reading sensitive keys, verify permissions with `PermissionChecker`:

```python
from agenthicc.security import PermissionChecker
from agenthicc.kernel import SecurityPolicy, PermissionRule

policy = SecurityPolicy(
    permission_rules=(
        PermissionRule(
            tool_pattern="global:embeddings/*",
            action="allow",
        ),
        PermissionRule(
            tool_pattern="project:secrets/*",
            action="deny",
        ),
    ),
    default_action="allow",
)

checker = PermissionChecker(policy)

# Check before reading
try:
    checker.check("project:secrets/api_key", agent_id="worker-1")
    value = router.read("project:secrets/api_key")  # only reached if allowed
except Rejection as e:
    print("Access denied:", e)
```

---

## Artifact sharing example: worker publishes, reviewer reads

```python
# worker_agent.py
import asyncio
from agenthicc.memory.router import MemoryRouter

async def run_worker(router: MemoryRouter, workflow_id: str) -> None:
    # ... run tests ...
    report_html = b"<html>... coverage report ...</html>"

    router.publish_artifact(
        workflow_id=workflow_id,
        name="coverage_report.html",
        data=report_html,
    )

    # Also write structured summary to project tier for fast reads
    router.write(
        f"project:artifacts/{workflow_id}/coverage_summary",
        {"total_coverage": 87.3, "failing_tests": 0},
    )


# reviewer_agent.py
async def run_reviewer(router: MemoryRouter, workflow_id: str) -> None:
    # Read the binary artifact
    data = router.read_artifact(workflow_id=workflow_id, name="coverage_report.html")
    if data is None:
        raise RuntimeError("Coverage report not yet published")

    # Read the structured summary
    summary = router.read(f"project:artifacts/{workflow_id}/coverage_summary")
    print(f"Coverage: {summary['total_coverage']}%")

    with open("coverage_report.html", "wb") as f:
        f.write(data)
```

---

## TTL and eviction

Session entries expire after `ttl_seconds` (set at `SessionMemoryLayer` construction).
`get` returns `None` for expired entries without removing them; call `prune_expired()`
periodically to reclaim memory:

```python
import asyncio

async def prune_loop(mem: SessionMemoryLayer, interval: float = 60.0) -> None:
    while True:
        removed = mem.prune_expired()
        if removed:
            print(f"Pruned {removed} expired session entries")
        await asyncio.sleep(interval)

asyncio.create_task(prune_loop(session_mem))
```

LRU eviction happens automatically when the cache is full (`maxsize` reached):
the least-recently-used entry is silently dropped.

---

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `read()` returns `None` unexpectedly | TTL expired or wrong key prefix | Check `ttl_seconds`; ensure prefix matches the tier |
| `sqlite3.OperationalError` | `db_path` parent directory doesn't exist | `ProjectMemoryLayer` creates the file but not parent dirs — create them first |
| `Rejection` on `checker.check` | Key matches a `"deny"` rule | Update the `SecurityPolicy` or use a different key |
| Artifact `None` on first read | Publisher hasn't run yet | Use `processor.drain()` + retry logic or dependency ordering in the DAG |
| LRU drops important entries | `maxsize` too small for workload | Increase `maxsize` or move long-lived data to the project tier |

---

## Key points

- Key prefixes are the routing contract: `session:`, `project:`, `global:`.
- Session layer is in-process only — it does **not** survive restarts.
- Project layer writes are immediate (SQLite WAL mode); they survive crashes.
- `publish_artifact` / `read_artifact` use `(workflow_id, name)` as the compound key.
- `PermissionChecker` uses `fnmatch` patterns — `"project:secrets/*"` blocks all
  keys under that path.
- `prune_expired()` does not affect LRU order — call it on a timer, not in the hot path.
- `MemoryRouter` does not enforce permissions — call `PermissionChecker.check` before
  `router.read` for sensitive keys.
