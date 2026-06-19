# PRD-101 — Three-Tier Memory Layers

## Problem

The `memory/` package (`SessionMemoryLayer`, `ProjectMemoryLayer`,
`GlobalMemoryLayer`, `MemoryRouter`, `SemanticIndex`) was fully implemented
and tested in isolation but never wired into the runtime.  Agents had no
persistent memory of past decisions, prior plans, or project context across
sessions.  `--resume` restored conversation history but not project-level
state.

---

## Goals

1. **Project continuity** — agents can recall past intents, approved plans, and
   project-specific knowledge across sessions.
2. **Cross-session semantic search** — agents can find semantically-similar past
   outputs to inform current work ("how did we solve this before?").
3. **Global user preferences** — user-wide settings and learnings persist across
   all projects.
4. **Agent-callable tools** — agents actively read and write memory without
   requiring system-prompt injection of pre-loaded context.

---

## Architecture

```
SessionContext / WorkflowConfig
  ├── session_memory   ShortTermMemory   (conversation history, token budget)
  ├── memory_router    MemoryRouter      (dispatches to all three layers)
  │     ├── session_layer  SessionMemoryLayer  in-process LRU+TTL
  │     ├── project_layer  ProjectMemoryLayer  .agenthicc/memory/project.db
  │     └── global_layer   GlobalMemoryLayer   ~/.agenthicc/global.db
  └── semantic_index   SemanticIndex     (TF-IDF / lauren-ai similarity search)
```

`ShortTermMemory` and `MemoryRouter` are complementary, not duplicates:
`ShortTermMemory` owns conversation message history and token budgeting;
`MemoryRouter` owns arbitrary key-value state and artifact persistence.

---

## Memory tiers

### Tier 1 — Session (`SessionMemoryLayer`)
In-process LRU cache, bounded at 1024 entries, optional per-key TTL.
Lost on process exit.  Used for turn-local caches (parsed mention results,
compiled skill bodies, temporary agent state).

### Tier 2 — Project (`ProjectMemoryLayer`)
SQLite-backed key-value store at `.agenthicc/memory/project.db`.  Namespaced
by agent or workflow.  Also exposes a content-addressed artifact table
(idempotent publish, retrieve by sha256 id).  Persists across sessions;
scoped to one project directory.

### Tier 3 — Global (`GlobalMemoryLayer`)
Same SQLite pattern at `~/.agenthicc/global.db`.  User-wide across all
projects.  Used for user preferences, cross-project learnings, shared
boilerplate templates.

### Semantic index (`SemanticIndex`)
In-process TF-IDF similarity index (wraps `lauren_ai._memory._vector` when
available; falls back to a built-in BoW implementation).  Every completed
agent turn is indexed by intent ID.  Agents query it via the
`semantic_search` tool to recall relevant past context.

---

## Agent tools

All four tools are injected into every interactive agent turn (Auto mode and
the plan phase of `code_plan`) via `make_memory_tools()`.

| Tool | Purpose |
|---|---|
| `memory_write(key, value, scope, namespace)` | Write to session / project / global memory |
| `memory_read(key, scope, namespace)` | Read from memory; returns `{"found", "value"}` |
| `semantic_search(query, top_k)` | Find similar past agent outputs by text similarity |
| `publish_artifact(content, content_type)` | Store content-addressed artifact in project layer; returns `artifact_id` |

`scope` is one of `"session"`, `"project"`, `"global"`.  Default is
`"project"`.  Session-scope TTL writes use `memory_write` with the optional
`ttl_seconds` argument (project/global ignore TTL).

---

## Semantic indexing

`AgentTurnRunner._stream()` indexes each completed turn's text into
`SemanticIndex` under the key `{intent_id}_{turn_n}`.  This happens
automatically — agents do not need to call any tool to trigger indexing.
The `semantic_search` tool lets agents query the index explicitly.

---

## Initialization

`_build_session_context()` in `tui_session.py` initialises the three layers
and router immediately after creating `ShortTermMemory`:

```python
project_memory = ProjectMemoryLayer(Path(".agenthicc") / "memory" / "project.db")
global_memory  = GlobalMemoryLayer()
session_layer  = SessionMemoryLayer()
memory_router  = MemoryRouter(session_layer, project_memory, global_memory)
semantic_index = SemanticIndex()
```

`ProjectMemoryLayer` creates `.agenthicc/memory/` on first init if absent.
Both layers use SQLite WAL mode for concurrent read access.

---

## File changes

| File | Change |
|---|---|
| `runners/session_context.py` | Add `memory_router`, `semantic_index` fields |
| `runners/agent_turn_context.py` | Add `memory_router`, `semantic_index` fields |
| `runners/agent_turn.py` | Add params to shim; auto-index turn text |
| `workflows/config.py` | Add `memory_router`, `semantic_index` fields |
| `workflows/memory_tools.py` | **New** — `make_memory_tools()` factory |
| `runners/tui_session.py` | Init layers; pass to SessionContext; inject tools |
| `workflows/code_plan/runner.py` | Add memory tools to `_base_tools()` |

---

## Acceptance criteria

- [ ] `.agenthicc/memory/project.db` is created on first session start.
- [ ] `memory_write(key="plan", value="…", scope="project")` persists across restarts.
- [ ] `memory_read(key="plan", scope="project")` returns the saved value in a new session.
- [ ] `semantic_search(query="auth module")` returns past turn IDs with similarity scores.
- [ ] `publish_artifact(content="…")` returns a stable `artifact_id`; calling again with identical content returns the same id.
- [ ] All existing tests pass.
- [ ] Memory tools appear in the agent's tool list in Auto mode and all `code_plan` phases.
