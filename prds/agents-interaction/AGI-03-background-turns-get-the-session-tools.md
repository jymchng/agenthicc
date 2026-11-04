---
title: "AGI-03: Background direct turns get the same session tools as the foreground — including ask_user"
status: Proposed
version: 1.0.0
date: 2026-11-04
scope: "one builder for the session-bound tools (ask_user, memory, browser), used by the foreground TUI turn and the background worker's direct turn"
related_prds: [AGI-00, AGI-01, AGI-02, PRD-100, PRD-227]
tags: [background-sessions, ask_user, tools, parity]
---

# AGI-03: Background direct turns get the session tools

## 1. Problem

AGI-00 §3.1.

- The foreground TUI passes every turn the session-bound tools: `ask_user`,
  `memory_read`/`memory_write`, `semantic_search`, `publish_artifact` and the
  browser tools (`tui_session._make_session_tools` + `ctx.browser_tools`,
  `tui_session.py:4560`).
- The background worker's direct turn passes only the project plugin tools
  (`worker._run_direct_turn`, `worker.py:1097`). A background agent without
  a workflow therefore:
  - cannot ask the operator anything. It writes its question in prose and
    ends the turn, or guesses;
  - has no session memory or browser tools, so the same request behaves
    differently in the background from the foreground.
- `BackgroundApprovalService.request_input` and the inbox already serve an
  `ask_user` from a background session. Only workflows reach them today, so
  this is a missing wire, not missing machinery.
- There are two hand-assembled tool lists. This is the drift CLAUDE.md warns
  against: "when a feature crosses the boundary, document and test the
  bridge".

## 2. Goals

1. **One builder.**
   - `runners/session_tools.py` gets `session_tools(ctx) -> list[ToolLike]`,
     returning the session-bound tools for a `SessionContext`:
     - `make_questions_tool(ctx.approval_svc)`;
     - `make_memory_tools(ctx.memory_router, ctx.semantic_index)`;
     - `ctx.browser_tools`.
   - `TUISession`'s turn, its `effective_tools` preview and
     `worker._run_direct_turn` all call it. `_make_session_tools` is deleted.
2. **Background answers go to the inbox.**
   - In a background session, `ctx.approval_svc` is the
     `BackgroundApprovalService`, so `ask_user` publishes a `question` item
     (AGI-01, AGI-02).
   - `--dangerously-skip-permissions` skips capability approvals, not the
     operator. Today the worker replaces the whole `BackgroundApprovalService`
     with `_HeadlessApprovalService` (`worker.py:1958`). That service cancels
     every question ("headless mode has no interactive question adapter").
   - Instead, the background service gains `auto_approve_capabilities`.
     Tool approvals are granted at once, and questions still publish to the
     inbox.
3. **Same agent, same catalog.** For the same agent and mode, a background
   direct turn offers the same tool names as a foreground turn, except the
   tools that need a terminal UI. Those are listed explicitly in the builder
   with the reason.

## 3. Non-goals

- Workflow phase tools. Workflows already add `ask_user` through
  `WorkflowConfig.approval_svc`.
- Headless `--headless` turns, which have no operator to ask. They keep the
  `approval_svc=None` cancellation.

## 4. Acceptance criteria

1. **Background `ask_user`.** A background direct turn whose scripted model
   calls `ask_user` produces a `question` inbox item. The session becomes
   `waiting_input`, and answering through the inbox returns the answers to
   the model. This is an integration test through `run_worker` with the
   production session builder and a scripted transport.
2. **Parity.** For the default agent in Yolo mode, the names a foreground
   turn offers equal the names a background direct turn offers, apart from
   the builder's terminal-only list. This test fails when either side adds a
   tool alone.
3. **Skipped permissions.**
   - In a background session started with `--dangerously-skip-permissions`,
     a write tool runs without an inbox item.
   - `ask_user` still publishes a `question` item and returns the operator's
     answers.
4. **One builder.** `_make_session_tools` appears nowhere in `src/`.

## 5. Rollout and security

- `ask_user` and the memory tools carry their existing capabilities
  (`control`, `read`/`write`). Background mode gates apply unchanged.
- `auto_approve_capabilities` covers capability approvals only. Workspace
  access outside the project stays denied without an explicit scope policy,
  exactly as `_HeadlessApprovalService` enforces today.
- Background agents gain tools, so their first-turn schema grows by the
  session-tool schemas. `/tools` in an attached session lists them.

## 6. Verification

- `uv run pytest tests/integration/test_background_sessions.py -q`
- New: `tests/integration/test_background_session_tools.py` (criteria 1–3)
  and a parity test in `tests/unit/test_session_tools.py` (criterion 2).
