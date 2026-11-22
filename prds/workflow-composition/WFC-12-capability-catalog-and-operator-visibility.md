---
title: "WFC-12: A capability catalog for the agent, and operator visibility for the stack and reloads"
status: Implemented
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P1
related_prds: [WFC-00, WFC-01, WFC-02, WFC-05, WFC-08, WFC-10, WFC-11, beau-5, agx-08, prd-114]
tags: [prompting, tui, commands, observability, discoverability]
---

# WFC-12 — Capability catalog and operator visibility

## 1. Summary

Two halves of one problem — *knowing what is available and what just happened*:

1. **For the agent:** a bounded, generation-aware catalog in the system prompt
   naming the workflows it can enter, the skills it can use and the tools added
   this session, so it can use what it (or a subagent) just created.
2. **For the operator:** a breadcrumb, transcript blocks, reload and authoring
   notices, and one command family to inspect and control the stack.

## 2. Problem (current source)

- Nothing tells the model which workflows exist or when to use them
  (`WorkflowPlugin` metadata is for the registry and `/workflow` picker).
  Without that, `enter_workflow` and `use_skill` would be rarely and poorly
  used, and a freshly created workflow, skill or tool would be undiscoverable
  unless the return packet happened to name it.
- The footer shows only the base selection (`conversation.workflow_override`).
  With a stack there is no indication of "I am in `create_tools` inside
  `default`".
- A reload today prints a transient string (`Tools reloaded — 5 tool(s) …`,
  `tui_session.py:4179-4228`); the diagnostics that matter — `untrusted`,
  `untrusted_modified`, `name_conflict` — have nowhere to be seen or acted on.
- `/workflow` has no way to show, return from or abandon a frame (WFC-01).

## 3. Goals

1. The model sees a compact catalog that is correct for the current generation
   and capability ceiling.
2. The catalog never destabilises the cached stable prompt prefix.
3. The operator can always see the stack, the last reload outcome, what was just
   published automatically and anything staged under `stage_only`, and can act on
   each from the keyboard or a command.
4. Every new command is in the one registry with picker and help coverage.

## 4. Non-goals

- A new TUI framework; the work extends existing workspace, overlay and
  transcript-block components (BEAU-5 workflow rendering, AGX-08 subagent panel).
- Rendering other sessions' stacks in the agents hub (data is available;
  presentation is a follow-up).

## 5. Design

### 5.1 The catalog

Built by `agents/catalog.py` from the live registries and the current policy
snapshot, placed **after** the stable prefix (system instructions, tool schemas)
in the volatile region so a catalog change does not invalidate the cached
prefix.

```text
## Available to you (generation 7)
Workflows — call enter_workflow(name, intent):
  create_tools — Create one or several new tools the session can call once published
  create_skills — Create reusable skills for this project
  create_workflow — Create or update a reusable workflow package
  release_notes — Draft release notes from merged PRs   [new this session]
Skills — call use_skill(name):
  release-notes — Draft release notes (use when: changelog, release)
New tools this session: fetch_rss, parse_rss
```

Rules:

- Only entries the caller may actually use under its frame, ceiling and agent
  filter (`WorkflowInvocation.agent_callable`, `filter_skills_for_agent`).
- Hard budget 1,500 characters. Overflow drops, in order: skill entries not
  matching recent user text, built-in workflows the model has already used this
  session, then oldest entries; it ends with
  `… N more (list_workflows / list_skills)`.
- `[new this session]` marks artifacts whose manifest entry is newer than
  session start (WFC-11), so freshly authored capabilities are salient.
- Regenerated when `ReloadService.generation` or the frame changes; otherwise the
  previous string is reused byte-for-byte.
- A worker's catalog omits `enter_workflow`'s workflows (WFC-02 FR-4) and keeps
  skills and new tools.

### 5.2 Breadcrumb and blocks

- **Footer/status line:** `default › create_tools · generate 2/4` (workflow
  names joined by `›`, current phase and position for the top frame). It is a
  projection of `AppState.workflow_frames` (WFC-03) — presentation state in
  `tui/conversation_store.py`, rendered by `tui/workspace/`.
- **Transcript blocks** (aligned with BEAU-5): *entered* (`↳ create_tools
  entered by agent — "create a feed-reading toolkit"`), *returned* (`↲ create_tools
  returned — 3 tools published and trusted · 3 loaded · undo: /authored rollback`), *abandoned*. The synthetic
  parent-resume turn is not drawn as user input.
- **Reload notice:** one line from `ReloadReport.summary()`, expandable to the
  diagnostics with path, code and digest (never file contents).
- **Publish notice:** after every automatic publish (WFC-11 §5.2) one
  non-blocking block names each artifact, kind, short digest, declared
  capabilities and hosts, advisory findings and `static_only`, with the undo
  command. It is informational and never waits for input.
- **Staged badge:** only under policy `stage_only`, an unpublished authored set
  shows `◆ 1 staged` in the status line until approved or rejected; it counts
  exactly what it names (the TUIR-05 lesson). Under the default policy there is
  no badge — the publish notice is the signal.

### 5.3 Commands

All registered in `commands/builtins.py` so completion, aliases, argument hints
and help come from the single registry (CLAUDE.md "Adding a slash command"):

| Command | Behaviour |
|---|---|
| `/workflow` | With no argument, show the stack (frames, status, phase). |
| `/workflow stack` | Same, with run ids and origins. |
| `/workflow call NAME <intent>` | A person pushes a call frame (WFC-01). |
| `/workflow return [--abandon]` | Abandon a paused child and return `abandoned` to its parent. |
| `/reload [surface…]` | Reload (WFC-05). |
| `/authored …` | List, diff, roll back, remove; approve and reject under `stage_only` (WFC-11). |
| `/catalog` | Print exactly what the agent currently sees (the §5.1 string). |

The command busy-policy for each (read-only vs. state-changing) follows the
existing `BusyPolicy` pattern used by `/skills` and `/mcp`.

### 5.4 Headless

The same data is emitted as JSON-line events: `workflow_frame`, `reload`,
`authored_published`. No TUI is required to inspect or, under `stage_only`,
approve (WFC-11 CLI).

## 6. Requirements

- **FR-1** `/catalog` output equals the string injected into the prompt for the
  same generation and frame (a test compares them).
- **FR-2** The catalog adds ≤ 1,500 characters and is byte-stable between
  reloads.
- **FR-3** No presentation code writes kernel state; the breadcrumb derives
  from the bridge in WFC-03 only.
- **FR-4** Unicode and colour fallbacks for the breadcrumb and blocks follow the
  existing terminal-fallback tests (non-TTY, narrow width, resize).
- **FR-5** Every new command has a picker-visibility test and an execution test.

## 7. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | After `create_tools` publishes `fetch_rss`, the next turn's prompt catalog contains `fetch_rss` and `[new this session]`; before the reload it does not. |
| A2 | With 60 workflows and 120 skills registered, the catalog is ≤ 1,500 characters and ends with the overflow line. |
| A3 | A reload that does not change any entry yields a byte-identical catalog (prompt-prefix cache unaffected). |
| A4 | The footer shows `default › create_tools · <phase>` while the child runs and `default` after it returns. |
| A5 | `/workflow stack`, `/workflow return --abandon`, `/reload`, `/catalog`, `/authored list` appear in the trigger picker and execute. |
| A6 | A reload with one `untrusted` file shows a notice naming the file, code and digest, and no file contents. |
| A7 | A `Plan`-mode session's catalog omits workflows it cannot enter. |
| A8 | After `create_tools` publishes, one non-blocking notice lists each tool with its short digest and declared capabilities and no input is requested; under `stage_only` the `◆ staged` badge appears instead. |

## 8. Test plan

- Unit: catalog budgeting and ordering, stability, filtering
  (`tests/unit/test_catalog.py`); breadcrumb formatting.
- Integration: catalog regeneration across a real reload; command registry
  coverage.
- TUI rendering tests under `tests/unit` using the existing workspace test
  helpers, including narrow-width and non-TTY.
- E2E: WFC-13 S1, S3–S5.

## 9. Documentation and cleanup

`docs/guides/tui.md`, `commands.md`, `workflows.md`, `llms-full.txt`;
`docs/glossary.md` gains *frame*, *return packet*, *generation*, *catalog*.
`conversation.workflow_override` and any footer code reading it are removed with
WFC-01.

## 10. Risks

| Risk | Mitigation |
|---|---|
| The catalog nudges the model to over-use workflows. | `when_to_use` is one sentence; approval and depth guards remain; the catalog is omitted when nothing is callable. |
| Prompt-cache invalidation. | Volatile placement, byte-stable reuse, A3. |
| Operator overload on busy sessions. | One-line notices by default; details on demand. |

## Amendment — 2026-11-22

The catalog uses the reload generation to refresh its sources and prints
identical text when the available entries are unchanged. The footer uses
the active terminal theme for its breadcrumb and staged indicator. The
operator can reopen diagnostic details with `/reload report`.


## Implementation status

| Criterion | Evidence |
| --- | --- |
| A1 | `test_create_tools_publishes_and_dispatches_on_the_first_parent_request` observes each new tool in the resumed model catalog. |
| A2 | `test_catalog_budget_preserves_matching_skills_and_new_tools`. |
| A3 | `test_catalog_command_matches_the_model_and_survives_an_unchanged_reload`. |
| A4 | `test_status_projects_nested_workflow_phase_and_staged_set_count` exercises the active and returned frames at several widths. |
| A5 | `test_picker_exposes_workflow_composition_controls`, `test_catalog_command_matches_the_model_and_survives_an_unchanged_reload`, `test_failed_child_keeps_selection_locked_until_operator_action`, and `test_authored_picker_and_operator_commands_restore_previous_behavior`. |
| A6 | `test_reload_report_shows_path_code_digest_and_no_file_contents`. |
| A7 | `test_plan_catalog_lists_only_eligible_read_only_workflows`. |
| A8 | `test_headless_publication_and_cli_approval` checks the publication and staging paths. `test_status_projects_nested_workflow_phase_and_staged_set_count` checks the staged badge. |
