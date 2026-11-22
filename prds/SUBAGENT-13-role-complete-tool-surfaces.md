---
title: "SUBAGENT-13: Tool surfaces that match the role, and a capability-aware type catalog"
status: Implemented (2026-11-22)
version: 0.2.0
date: 2026-11-22
scope: "the investigative roles' read-only allow-lists, the model-facing catalog of types in the spawn_subagents description, refusal of a type that cannot do its job in the current session before any provider call, and what counts as a workspace mutation"
area: A-10
related_prds:
  - SUBAGENT-01
  - SUBAGENT-05
  - SUBAGENT-12
  - SUBAGENT-14
  - PRD-26
  - PRD-124
  - PRD-197
tags:
  - subagents
  - tools
  - capabilities
  - catalog
---

# SUBAGENT-13: Tool surfaces that match the role, and a capability-aware type catalog

## Revision 0.2.0 (2026-11-22)

0.1.0 proposed widening the working roles' hand-written tool lists. The
requester's direction — *subagents must behave like the main agent* — makes that
unnecessary: SUBAGENT-12 §5.2 gives `implementer`, `executor`, `tester` and
`documenter` **every tool the parent exposes** (`allowed_tools=None`), so there
is no list left to keep complete. This revision keeps only what still applies:
the investigative roles, the parent's view of the catalog, early refusal, and
the definition of "mutation".

## Implementation status (2026-11-22)

Implemented as specified; criteria 1–7 are tests in
`tests/unit/test_subagent_tool_surfaces.py` and
`tests/unit/test_subagent_outcomes.py`. `reviewer` and `verifier` are tagged
`runs commands` rather than `read-only` because they hold `run_python_expr` /
`run_tests`; the tag reports capability, not intent.

## 1. Executive summary

Three problems survive inherit-all:

1. **The investigative roles are missing read tools their prompts need.** A
   `reviewer` told to review a commit has no `git_show`; a `planner` has no
   `git_log`; none can `batch_read` several files in one call.
2. **The parent chooses types blind.** The `spawn_subagents` description lists
   `- implementer: Implement a narrowly scoped code change and verify it.` with
   no indication of whether a type can modify files or is usable at all in the
   current mode. A Plan-mode parent is offered `implementer`; the worker starts,
   then fails.
3. **"Mutating" is wider than the workspace.** `_is_mutating_tool` treats any
   tool with the `write` capability as a file mutation, so `send_email`,
   `reply_email`, `move_email` and `create_event` (all `network` + `write`)
   would count as evidence of a file change and be recorded as `changed_paths`.

## 2. Problem statement

Measured against the current code, the tool schema each investigative worker
receives is:

```
explorer    read_file list_directory search_files grep_files git_log git_show git_blame git_grep get_file_info
planner     read_file list_directory search_files grep_files
reviewer    read_file list_directory search_files grep_files git_diff git_log run_python_expr
verifier    read_file search_files grep_files git_diff git_log run_python_expr run_tests
researcher  read_file search_files grep_files list_directory
```

Registered read tools they cannot reach although the role needs them:

| Role | Missing, with the prompt sentence that needs it |
|---|---|
| explorer | `batch_read`, `git_diff`, `git_status` ("inspect git history", "gather facts") |
| planner | `batch_read`, `get_file_info`, `git_log`, `git_status` ("read the codebase") |
| reviewer | `git_show`, `git_blame`, `git_status`, `batch_read`, `get_file_info` ("review the code or change") |
| verifier | `git_show`, `git_status`, `batch_read`, `get_file_info` ("read files and grep") |
| researcher | `batch_read`, `get_file_info` ("search local files and documentation") |

## 3. Current-source investigation

* `types.py` holds one `frozenset` per investigative role.
* `pool.py` `_expand_allowed` expands globs through the `ToolRegistry`; an
  unknown literal expands to the empty set (`ToolRegistry.glob_expand` returns
  `frozenset()`), so a typo in a list silently shrinks the tool set. Today every
  name is registered — verified — but nothing enforces it.
* `tool.py` `_augment_type_schema` builds the type catalog from
  `spec.description` only. It has the registry but not the visible tools or the
  policy, which `make_spawn_subagents_tool` has at the call site.
* The only "no usable tool" check is a `RuntimeError` inside
  `SubagentWorker._execute`, after the pool, panel row and ledger entry exist.
* In a Plan-mode parent with all tools visible, write tools *are* shown to an
  inherit-all worker; every call is then refused by `ToolCapabilityGate` with
  `blocked_by_mode`. The model spends turns on calls that cannot succeed.

## 4. Goals

* **G1.** Each investigative role's list covers the read verbs in its prompt and
  contains no write- or execute-capable tool except where its role runs checks.
* **G2.** A drift test fails if a built-in list names an unregistered tool, a
  read-only role gains a write tool, or an evidence-requiring role has no tool
  that can satisfy its rule.
* **G3.** The parent sees, per type, what it can do **in this session**:
  `read-only`, `edits files`, `runs commands`, or `unavailable: <why>`.
* **G4.** A task for a type that cannot do its job here is refused before any
  worker, panel row or provider call, with the reason.
* **G5.** Only tools that change the **workspace** count as mutations.

## 5. Design

### 5.1 Investigative allow-lists

Only additions; every existing tool is kept.

| Type | Added |
|---|---|
| explorer | `batch_read`, `git_diff`, `git_status` |
| planner | `batch_read`, `get_file_info`, `git_log`, `git_status` |
| reviewer | `batch_read`, `get_file_info`, `git_show`, `git_blame`, `git_status` |
| verifier | `batch_read`, `get_file_info`, `git_show`, `git_status` |
| researcher | `batch_read`, `get_file_info` |

All are `read` or `git_read` capability tools. The working roles carry no list
(SUBAGENT-12 §5.2).

### 5.2 One usability rule

`subagents/pool.py` holds one function used by the worker, the admission check
and the catalog:

```python
def unusable_reason(spec, tools, blocked_capabilities=frozenset(), *, mode_name="") -> str: ...
def tool_tags(tools, blocked_capabilities=frozenset()) -> tuple[str, ...]: ...
```

`unusable_reason` is `""` unless `spec.completion_evidence` (SUBAGENT-12) cannot
be satisfied: `mutation` needs a usable mutating tool; `action` needs a usable
mutating or execute tool. "Usable" means present **and** not hard-blocked by the
session policy's `blocked_capabilities`. The worker's inline `RuntimeError` and
its `"implementer"` name test are replaced by this call: one rule.

### 5.3 Mutation means workspace mutation

`_is_mutating_tool` becomes: name in `_MUTATING_TOOL_NAMES`, or capability
`write` present **and** `network` absent. `send_email`, `reply_email`,
`move_email`, `create_event` stop counting as file evidence.

### 5.4 Capability-tagged catalog

`_augment_type_schema` receives a `Mapping[str, str]` of precomputed tags:

```
- explorer [read-only]: Read-only codebase exploration and evidence gathering.
- reviewer [runs commands]: Review code for correctness, clarity, security issues, and bugs.
- implementer [edits files, runs commands]: Implement a narrowly scoped code change and verify it.
- implementer [unavailable in this session: implementer needs a file-changing tool but the Plan policy blocks it]: …
```

Tags are computed from the tools the type would actually receive: its
allow-list (glob-expanded; `None` = all visible) ∩ the visible tools, minus tools
whose capabilities the policy blocks, minus the type's `permissions.deny_tools`.

### 5.5 Admission refusal

After task parsing and before pool construction, `spawn_subagents` returns
`{"ok": False, "status": "refused", "error": "...", "unavailable_types": [...]}`
for a task whose type has an `unusable_reason`. No pool id, panel row, ledger
entry or provider call exists. A mixed pool is refused as a whole, naming each
unusable type, so the parent re-plans instead of receiving a half-run pool.

### 5.6 Drift test

`tests/unit/test_subagent_tool_surfaces.py` asserts, for every built-in spec: all
explicit `allowed_tools` are registered names; `explorer`, `planner`,
`reviewer`, `verifier`, `researcher` hold no tool with `write`/`git_write`
capability and no execute tool except the ones they already had
(`run_python_expr`, `run_tests`); the additions in §5.1 are present; the working
roles declare `allowed_tools is None`.

## 6. Non-goals

* No new tools and no change to any tool's behaviour or capability.
* No per-call tool override on `spawn_subagents` (PRD-197 rejects
  model-supplied policy arguments).
* No change to how Markdown definitions declare `tools`/`capabilities`.
* No change to nested-spawn depth tiers (`depth_tier.allowed_tools`), which
  continue to narrow the result.

## 7. Acceptance criteria

1. The investigative lists are exactly as in §5.1 and the drift test passes.
2. The drift test fails when an unregistered name is added to a built-in list,
   and when a write-capable tool is added to a read-only role.
3. In a Plan-mode parent, `spawn_subagents(tasks=[{"type":"implementer",…}])`
   returns `ok=False, status="refused"` with `unavailable_types == ["implementer"]`,
   and the transport saw **zero** provider calls.
4. In a Plan-mode parent, a `reviewer` task still runs.
5. The `spawn_subagents` description shows `[read-only]` for explorer, planner
   and researcher, `[runs commands]` (no `edits files`) for reviewer and
   verifier, `edits files, runs commands` for implementer in a Yolo session,
   and `[unavailable in this session: …]` for implementer in a Plan session.
6. `send_email` with a successful result is not recorded in `changed_paths` and
   does not satisfy `mutation` evidence.
7. `SubagentWorker` contains no `"implementer"` literal and no
   "no write-capable tools" `RuntimeError`.

## 8. Verification

Unit tests above; existing integration tests that build custom
`_implementer_registry("write_file")` keep passing because `unusable_reason` is
satisfied by `write_file`. `llms-full.txt` and `docs/guides/subagents.md` get the
catalog tags and the refusal.

## 9. Related PRDs

* SUBAGENT-12 defines inherit-all and `completion_evidence`, which this PRD's
  refusal reads.
* SUBAGENT-14 reports the surface a worker actually had.
* PRD-26 (agent-scoped tools) is the origin of per-type allow-lists.
