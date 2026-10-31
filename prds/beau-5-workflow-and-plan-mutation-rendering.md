---
title: "BEAU-5: Workflow and Plan Mutation Render Blocks"
status: Proposed
version: 2.0.0
created: 2026-10-31
scope: "specify render blocks for goal_list_mutated, phase_control_requested, phase_plan_mutated, and phase_control_rejected"
related_prds: [BEAU-1, PRD-185, PRD-194, AGX-11]
tags: [tui, scrollback, workflows, plans, accessibility]
---

# BEAU-5 — Workflow and Plan Mutation Render Blocks

## 1. Problem and objective

Goal-list and phase-control events represent durable workflow changes, not
ordinary assistant prose. Their display must distinguish a request from an
accepted transition, a committed plan mutation, and a rejected request. If
these states collapse into a generic green “updated” banner, the user may
believe a phase moved when the request is pending or was rejected.

This PRD defines a compact block for each event kind. It changes only the visual
projection; workflow state, event order, safe-boundary rules, and checkpoint
semantics remain authoritative elsewhere.

## 2. Research applied

- **Codex CLI** documents permission/status inspection and inspecting commands
  and diffs, but no exact transcript format. Keep action and outcome explicit;
  do not call a request a completed action.
- **OpenCode** identifies permission requests, questions, errors, and completed
  sessions as distinct attention states. Borrow explicit status words, not its
  notification behavior.
- **Qoder** documents a task list and review of commands/results/diffs, but the
  task-progress layout is unspecified. Keep concise progress in scrollback and
  leave full plan detail to existing workflow views.
- **DeepSeek Harness** documents plans and permission-gated actions in a
  workspace session, but not a TUI layout. Borrow the distinction between
  plan and approval boundaries only; no DeepSeek visual is inferred.
- **Claude Code** supports transcript review and a separate diff panel in its
  fullscreen renderer. Do not add mutable panels or search to scrollback.
- **Grok Build** documents bottom live context, not workflow mutation rows. Keep
  durable event rows separate from the live strip.

Sources: [Codex CLI](https://learn.chatgpt.com/docs/codex/cli),
[OpenCode TUI](https://opencode.ai/docs/tui/),
[Qoder CLI](https://docs.qoder.com/cli/interface.md),
[DeepSeek Harness guide](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart),
[Claude Code fullscreen](https://code.claude.com/docs/en/fullscreen), and
[Grok Build](https://docs.x.ai/build/overview). Full source distinctions are in
BEAU-1.

## 3. Event scope and semantic distinctions

| Event | Current payload/use | Semantic state shown |
|---|---|---|
| `goal_list_mutated` | `operation`, `index`, `goal_count`; helper avoids opaque goal ID/text and validates positions. | Committed list change. |
| `phase_control_requested` | `operation`, bounded `phase_id`, `reason`; safe-boundary request. | Requested/pending control, not a committed mutation. |
| `phase_plan_mutated` | `operation`, bounded `phase_id`, `disposition`, `reason`; committed plan mutation. | Applied change and resulting disposition. |
| `phase_control_rejected` | `operation`, bounded `phase_id`, `error_code`. | Explicit rejection; no mutation. |

Use only fields authorized by the current event contract (PRD-194). Do not
render full goal text or opaque goal identifiers. Unknown operations may use a
neutral human-readable fallback; they must not imply success.

## 4. Normative render blocks

Examples show plain text; styling is supplementary.

### 4.1 `goal_list_mutated`

Insert:

```text
  ⎿ Plan updated
      Inserted a goal · position 2 of 4
      ↳ Current goal continues
```

Append:

```text
  ⎿ Plan updated
      Added a goal to the end · position 4 of 4
      ↳ Current goal continues
```

When the event has a bounded count but no coherent position:

```text
  ⎿ Plan updated
      Added a goal · plan now has 4 goals
      ↳ Current goal continues
```

Only display one-based position when `index` and `goal_count` are coherent;
append must occupy the final position. Do not add goal text/ID, infer a title,
or display contradictory/oversized values. Keep the block together with one
trailing blank separator.

### 4.2 `phase_control_requested`

```text
  ⎿ Requested: postpone phase build at the next safe boundary
      Reason: Finish the active validation step first
```

Without a reason:

```text
  ⎿ Requested: bring phase review forward at the next safe boundary
```

The word `Requested` is mandatory. Do not label this event `Postponed phase`
or imply that the plan has changed. Keep the operation and bounded phase ID
visible; preserve the event's reason within its existing 160-character bound.
Use neutral/attention styling, never success green. If verified schema wording
differs, use the event contract's exact meaning.

### 4.3 `phase_plan_mutated`

```text
  ⎿ Phase plan updated: postpone build → queued · checkpointed
      Finish the active validation step first
```

```text
  ⎿ Phase plan updated: delete docs → removed · checkpointed
```

Show operation, bounded phase ID, and actual `disposition`. Preserve the word
`checkpointed` only if the event contract confirms this mutation is committed
and checkpointed. Do not add an empty reason row. A checkpoint does not mean
the workflow itself succeeded; keep success styling scoped to the confirmed
plan mutation.

### 4.4 `phase_control_rejected`

```text
  ⎿ Phase control rejected: postpone build (active_phase)
```

Use `error_code` as the bounded diagnostic code; do not invent a natural
language reason absent from the event. Preserve operation and phase ID as
subordinate context. Use an explicit `rejected` word with failure/attention
styling; never render this as a successful update.

### 4.5 Request → commit / rejection sequences

```text
  ⎿ Requested: postpone phase build at the next safe boundary

  ⎿ Phase plan updated: postpone build → queued · checkpointed
      Finish the active validation step first
```

```text
  ⎿ Requested: delete phase docs at the next safe boundary

  ⎿ Phase control rejected: delete docs (active_phase)
```

These are separate canonical events. Preserve actual order. Do not merge them
across event IDs, infer correlation beyond available fields, or suppress the
request in favor of the later result.

## 5. Hierarchy, spacing, and width behavior

- Use one consistent tree marker, shared with other subordinate event blocks;
  do not draw a full box around a one- or two-row notice.
- Put request/mutation/rejection first; place reason/disposition beneath it.
- A block contains only one event and gets one trailing blank separator.
- Use display-cell widths at 40/48/64/80/120/200 columns. Preserve state word,
  operation, and phase ID. Wrap long reasons under the reason text; only shorten
  lower-priority reason text with an explicit ellipsis.
- Escape payload strings as literal data, never Rich markup. ANSI-off and
  screen-reader output must speak `Requested`, `Plan updated`, and
  `Phase control rejected`; glyphs and color are supplemental.

## 6. Research-to-decision traceability

| Evidence | Applied decision |
|---|---|
| OpenCode distinguishes permission/question attention, error, and completion. | Request, mutation, and rejection get different explicit words/severity. |
| Codex exposes permissions/status but not precise row grammar. | Keep intent and outcome explicit without borrowing undocumented styling. |
| Qoder has a task-list entry point; task-progress details are unspecified. | Keep scrollback concise; do not invent a task dashboard. |
| DeepSeek Harness documents plans/approval but no terminal layout. | Borrow only the conceptual distinction, not visual styling. |
| Claude Code offers transcript review and diff panel in fullscreen. | Keep each mutation self-contained in native scrollback. |
| Grok's documented context line is separate from transcript. | Do not use live status as a substitute for durable mutation output. |

## 7. Non-goals and safety

- Changing workflow transitions, safe-boundary rules, authorization, plan
  persistence, checkpoints, rejection policy, event schemas, or order.
- Adding a second workflow source of truth, correlation cache, dashboard, modal,
  retry, or completion recap.
- Printing full goal text or hidden plan/reasoning data.
- Reformatting subagent/completion events owned by PRD-255/257.

## 8. Acceptance criteria

1. All four event kinds have exact snapshots matching §4, asserting wording,
   state, order, indentation, and separators separately from color.
2. `phase_control_requested` reads as a request, never an applied mutation.
3. `phase_plan_mutated` reflects actual disposition and only claims checkpointing
   when the event contract confirms it.
4. Rejected control is visibly rejected and does not imply a plan change.
5. Goal positions are one-based only for coherent bounded values; opaque IDs and
   goal text are absent.
6. Long IDs/reasons remain legible at 40/48/64/80/120/200 columns; ANSI-off and
   screen-reader output preserve state words.
7. Tests cover supported/unknown operations, missing/malformed fields,
   empty/long reasons, oversized/incoherent counts, rejection, replay, and
   request/mutation sequence order.
8. Rendering changes no reducer state, checkpoint, event payload, transition,
   or event order.

## 9. Verification

Add snapshots for goal append/insert, every phase operation, rejection, and
request→mutation sequences. Verify payload meanings against PRD-194 before
changing copy. Test live/replay parity, ANSI-off, and narrow widths. No provider
or external workflow is needed.
