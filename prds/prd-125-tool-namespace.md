# PRD-125 — Tool Namespace

## Problem

The 50 built-in tools plus any MCP and plugin tools are presented to the LLM
as a single flat bullet list injected into every agent turn.  Three concrete
problems follow from this flat structure:

1. **Signal density.** `### Available Tools\n- read_file: ...\n- write_file: ...`
   (50 items, ~2,750 chars) gives the model no domain structure.  All tools
   appear equally relevant regardless of the task.

2. **Collision blindness.** A plugin that exports `read_file` or `run_bash`
   silently replaces the built-in via last-writer-wins with a DEBUG-only log.
   Operators have no visible warning that a core tool has been shadowed.

3. **Subagent spec brittleness.** `SubagentTypeSpec.allowed_tools` is a
   `frozenset[str]` of raw `__name__` values.  Adding a new FS tool requires
   editing every spec that should allow FS access.  There is no glob or
   wildcard shorthand.

## Goals

- Group tools by domain in the system prompt (File System, Git, Shell / Exec,
  Outlook, Additional).
- Emit a WARNING when a plugin shadows a built-in from a *different* group.
- Allow `SubagentTypeSpec.allowed_tools` to contain glob patterns
  (`"fs.*"`, `"git.*"`) expanded at pool-creation time.
- Zero changes to lauren-ai, zero changes to the model-facing tool name.

## Non-goals

- Renaming tools (no `fs.read_file`).
- Changing `ToolMeta.name` in lauren-ai.
- Changing the JSON schema sent to the model.

## Solution

### 1. `ToolGroup` dataclass — `plugins/registry.py`

```python
@dataclass
class ToolGroup:
    name:        str        # machine key: "fs", "git", "exec", "outlook", "mcp:github"
    label:       str        # human-readable: "File System"
    description: str        # one-line summary for the section header
    tools:       list[PluginTool]
    priority:    int = 0    # display order; higher = rendered first
```

### 2. `ToolRegistry` extensions

**`register_group(group, *, source)`** — registers all tools in a `ToolGroup`
and records the group membership in a new `_tool_groups: dict[str, str]`
mapping (`tool_name → group.name`).

**`register(tool, *, source, group)`** — existing logic extended: when a tool
name already exists *and* the new tool comes from a different group than the
existing one, log at WARNING level.

**`glob_expand(pattern) -> frozenset[str]`** — expands `"fs.*"` to all tool
`__name__` values in group `"fs"`.  Returns `frozenset({pattern})` for literal
names (backward compatible).

**`describe() -> str`** — now groups tools into Markdown sections:

```
### File System (24 tools)
_Read, write, search, and patch files._
- **read_file**: Read the full contents of a file.
...

### Git (11 tools)
_Query and commit the repository._
- **git_status**: Show working tree status.
...

### Additional Tools
- **mcp_search_web**: Search the web via Brave API.
```

### 3. Built-in group constants — `agent_tools.py`

```python
FS_GROUP     = ToolGroup("fs",      "File System",       "Read, write, search, and patch files.",   FS_AGENT_TOOLS,      priority=4)
GIT_GROUP    = ToolGroup("git",     "Git",               "Query and commit the repository.",        GIT_AGENT_TOOLS,     priority=3)
EXEC_GROUP   = ToolGroup("exec",    "Shell / Exec",      "Run commands, scripts, and tests.",       EXEC_AGENT_TOOLS,    priority=2)
OUTLOOK_GROUP = ToolGroup("outlook","Outlook / Calendar","Email, calendar, and Graph API.",         OUTLOOK_AGENT_TOOLS, priority=1)

BUILTIN_GROUPS: list[ToolGroup] = [FS_GROUP, GIT_GROUP, EXEC_GROUP, OUTLOOK_GROUP]
```

`build_registry()` calls `register_group(g)` for each builtin group instead
of `register_many(AGENT_TOOLS)`.

### 4. MCP group registration

In `build_registry()`, MCP tools are not yet grouped.  After this PRD, MCP
tools passed via `project_plugin_tools` with a server-name attribute are
registered under `mcp:{server_name}` groups.  (Full MCP grouping is a
follow-on; in v1 MCP tools appear in "Additional Tools".)

### 5. Subagent glob expansion — `subagents/pool.py`

`SubagentWorker._execute()` and `SubagentPool.run()` already have access to
`all_tools` (the full list) and `spec.allowed_tools`.  A helper
`_expand_allowed(allowed_tools, all_tools, registry)` is added that:

1. For each pattern in `allowed_tools` ending in `.*`, calls
   `registry.glob_expand(pattern)` to get the expanded name set.
2. For literal names, keeps them unchanged.
3. Returns the union as a `frozenset[str]`.

`SubagentWorker.__init__` receives an optional `registry: ToolRegistry` so the
expansion can happen at worker-creation time.  Fallback when no registry is
provided: treat `.*` patterns as a literal name (backward compatible).

`SubagentTypeSpec.allowed_tools` typing stays `frozenset[str]` — globs and
literals are both valid strings.

## Acceptance criteria

| # | Criterion |
|---|---|
| 125.1 | `ToolGroup` dataclass exists with `name`, `label`, `description`, `tools`, `priority` |
| 125.2 | `ToolRegistry.register_group()` registers all tools and records group membership |
| 125.3 | `ToolRegistry.glob_expand("fs.*")` returns all FS tool names |
| 125.4 | `ToolRegistry.glob_expand("git_status")` returns `frozenset({"git_status"})` (literal pass-through) |
| 125.5 | Cross-group collision emits a WARNING; same-group override stays at DEBUG |
| 125.6 | `describe()` groups tools into labelled sections with count and description |
| 125.7 | `build_registry()` uses `register_group()` for all four builtin groups |
| 125.8 | `BUILTIN_GROUPS` is exported from `agent_tools.py` |
| 125.9 | Subagent `allowed_tools=frozenset({"fs.*"})` expands to all 24 FS tool names |
| 125.10 | All existing unit tests still pass |

## Files changed

| File | Change |
|---|---|
| `src/agenthicc/plugins/registry.py` | Add `ToolGroup`; extend `ToolRegistry` with `_tool_groups`, `register_group`, `glob_expand`, grouped `describe` |
| `src/agenthicc/agent_tools.py` | Add `FS_GROUP`, `GIT_GROUP`, `EXEC_GROUP`, `OUTLOOK_GROUP`, `BUILTIN_GROUPS` |
| `src/agenthicc/subagents/pool.py` | Add `_expand_allowed()`; pass registry to workers |
| `src/agenthicc/subagents/types.py` | Document glob syntax in `allowed_tools` field comment |
| `tests/unit/test_tool_namespace.py` | New tests for all acceptance criteria |
