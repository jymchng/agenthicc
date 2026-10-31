---
title: "BEAU-1: Scrollback Art Direction, Research, and Full Renderer Map"
status: Proposed
version: 2.0.0
created: 2026-10-31
scope: "define the shared visual system and explicit render contract for every ScrollBufferAppender output, including adjacent renderer families owned by existing PRDs"
related_prds:
  - AGX-11
  - PRD-157
  - PRD-161
  - PRD-255
  - PRD-256
  - PRD-257
  - BEAU-2
  - BEAU-3
  - BEAU-4
  - BEAU-5
tags:
  - tui
  - scrollback
  - design-system
  - ux
  - accessibility
  - research
---

# BEAU-1 — Scrollback Art Direction, Research, and Full Renderer Map

## 1. Executive summary

`ScrollBufferAppender` prints conversation events above the persistent live
workspace. It is the one component responsible for conversation-event terminal
writes and sends replayed events through the same rendering path. The appender
has 23 registered event kinds, a separate idle-header method, and two distinct
tool compression behaviors. In addition, several user-visible rows in the same
workspace are owned by adjacent PRDs: exploratory grouping (PRD-161), subagent
activity (PRD-255), status and usage (PRD-256), and completion recaps (PRD-257).

This series defines one coherent, evidence-based art direction for all of those
surfaces. BEAU-2 through BEAU-5 specify concrete blocks for the core narrative,
tool/file output, notices/recovery, and workflow mutations. This master PRD
contains the complete event-to-owner inventory and the integration blocks for
existing owner PRDs so no surface is left without an explicit visual contract.

The design target is **editorial, text-first terminal output**: a calm transcript
with clear conversational hierarchy, confident operation/result labels,
restrained color, concise metadata, bounded detail, and excellent native
scrollback readability. It should feel deliberately composed, not like a log
stream or a stack of individually boxed cards.

This is an art-direction proposal, not a second runtime. It does not replace
`ScrollBufferAppender`, event replay, Rich, `LiveRegion`, AGX-11, or the PRD owners
listed above.

## 2. Source inventory and rendering ownership

Reviewed `src/agenthicc/tui/workspace/appender.py` and adjacent owner PRDs on
2026-10-31. The registered kinds below are the complete current appender
registry. A new renderer must be added to this table, or explicitly classified
as intentionally non-rendered, in the same change that registers it.

| Kind / surface | Render role and current payload facts | Visual owner |
|---|---|---|
| `turn_start` | Agent name and event timestamp; opens an assistant turn. | BEAU-2 |
| `user_message` | User's literal message text. | BEAU-2 |
| `text` | Assistant Markdown text; blank text is suppressed. | BEAU-2 |
| `thinking_step` | Existing visible step text plus `done`; this is progress copy, not hidden reasoning. | BEAU-2 |
| `mention_chips` | Resolved chips with raw mention, kind, and success flag; current verbs are Read, Listed, Fetched. | BEAU-2 |
| `tool_complete` | Operation, bounded arguments/output preview, state, duration, and optional exploratory presentation metadata. | BEAU-3; PRD-161 owns exploratory grouping behavior. |
| `file_modified` | Path, tool, and available before/after lines; uses `diff_renderer`. | BEAU-3 |
| `error` | Safe message and optional bounded detail. | BEAU-4 |
| `system` | Literal system text, currently dim. | BEAU-4 |
| `announcement` | Literal plain text, used for screen-reader announcements; not Rich markup. | BEAU-4; accessibility contract is immutable. |
| `tool_recovery` | Bounded interrupted-tool recovery copy. | BEAU-4 |
| `mcp_server_failure` | Redacted failure message, optionally marked required. | BEAU-4 |
| `network_retry` | Attempt/limit, retry delay, bounded reason/status/detail. | BEAU-4 |
| `provider_recovery_retry` | Attempt/limit, delay, provider status, optional detail. | BEAU-4 |
| `goal_list_mutated` | Safe mutation summary; intentionally avoids goal ID and goal text. | BEAU-5 |
| `phase_control_requested` | Operation, bounded phase ID, safe-boundary wording, optional bounded reason. | BEAU-5 |
| `phase_plan_mutated` | Operation, bounded phase ID, disposition, reason; committed mutation output. | BEAU-5 |
| `phase_control_rejected` | Operation, bounded phase ID, error code. | BEAU-5 |
| `turn_complete` | Per-turn elapsed seconds; PRD-257 owns duration semantics and recap. | PRD-257 |
| `activity_complete` | Whole user-activity elapsed seconds; PRD-257 owns duration semantics and recap. | PRD-257 |
| `subagent_pool_started` | Worker labels/tasks, reported total; appender limits initial preview. | PRD-255 |
| `subagent_worker_done` | Worker status, progress counts, duration, summary or changed paths/error. | PRD-255 |
| `subagent_pool_done` | Succeeded/total, combined unsuccessful count, optional budget reason. | PRD-255 |
| `print_idle_header()` | Separate method: session ID, working directory, turn/cost/usage quality and divider. | PRD-256 / PRD-157 |
| Persistent subagent panel | Live worker rows below the composer border, not a scrollback event. | PRD-255 / AGX-08 |
| Parent status and usage strip | Live activity, elapsed, worker count, usage, identity/status. | PRD-256 / PRD-157 / AGX-11 |
| Generic tool overflow | More than `max_live_tool_calls` in a contiguous tool group. | BEAU-3 visual grammar; preserve current appender counting. |
| Exploratory group | Contiguous successful events marked `presentation.exploratory`; bounded child list. | PRD-161 behavior; BEAU-3 shared visual tokens. |
| `prompt_cache` diagnostic | Produced internally but not registered as a scrollback renderer. | Intentionally invisible; no new output in this series. |
| Unknown event kind | `_render_one` ignores it. | Remains silent until explicitly assigned and tested. |

### 2.1 Ownership rule

BEAU-1 establishes visual tokens and review criteria; it does not change the
following behavioral owners:

- **AGX-11:** live-frame scheduling, render caching, diffing, synchronized
  writes, and append-safe redraw policy.
- **PRD-157:** exact usage values, cost, accounting, and data quality.
- **PRD-161:** exploratory classification, group boundaries, counts, replay,
  raw-event granularity, and feature behavior.
- **PRD-255:** subagent pool lifecycle, counts, labels, task previews, panel,
  worker rows, and navigation.
- **PRD-256:** status strip, idle header, usage quality, identity rows, and
  width priorities.
- **PRD-257:** turn/whole-activity time scopes, duration formatting, and recap
  grouping.

When visual wording conflicts with those contracts, update the owning PRD. Do
not clone their counters or add a second rendering state machine.

## 3. Product research and evidence

Research was done against official product documentation and official project
repositories, accessed 2026-10-31. Each row separates documented facts from
Agenthicc recommendations; no unstated third-party visual convention is
attributed to a product.

| Product / primary source | Verified patterns | What Agenthicc should borrow | Limits; do not claim or copy |
|---|---|---|---|
| **Claude Code** — [Fullscreen rendering](https://code.claude.com/docs/en/fullscreen), [Interactive mode](https://code.claude.com/docs/en/interactive-mode), [Accessibility](https://code.claude.com/docs/en/accessibility) | Its fullscreen mode has a fixed-bottom input, visible-message rendering, mouse selection, expandable collapsed tool results, transcript search/navigation, `/focus`, diff review, and paused auto-follow with a jump-to-bottom/new-message count. The classic renderer preserves native terminal scrollback. Screen-reader mode normally uses classic rendering. | A concise primary row with optional bounded details; separate transcript and live controls; legible native scrollback; never let tool output masquerade as conversation. Use its reversible-detail principle only where an existing Agenthicc UI can actually expand content. | Do not promise click-to-expand, mutable scrollback, alternate-screen search, or a fullscreen prompt. These BEAU specifications target append-only scrollback. Do not copy brand glyphs or exact copy. |
| **OpenCode** — [TUI documentation](https://opencode.ai/docs/tui/) | Documents a tool-detail visibility toggle (`/details`), a `diff_style` that can adapt automatically or use stacked rows, configurable themes, and attention notifications for questions, permission requests, errors, and completed sessions. The page does not specify a full transcript grammar. | Make primary operation/status visible and subordinate details optional in existing navigation; use one-column/stacked diffs where width is constrained; make warning/approval/completion states semantically distinct. | Do not infer a documented transcript row layout, status line, screen-reader contract, or theme palette from this page. No new details toggle is in scope. |
| **OpenAI Codex CLI** — [CLI guide](https://learn.chatgpt.com/docs/codex/cli) | The guide shows an interactive CLI with model and working-directory context, a prompt area, and command suggestions. It encourages inspecting commands and diffs as work appears, exposes session status and permissions, and provides non-interactive `codex exec`. It does not specify exact transcript row styling or diff syntax. | Keep command/action/result and change summaries inspectable; show status/context separately from the narrative; make permission and failure states explicit. | Do not attribute particular colors, borders, transcript grouping, line counts, or output-preview behavior to Codex when the guide does not specify them. |
| **Qoder** — [CLI interface and shortcuts](https://docs.qoder.com/cli/interface.md), [CLI overview](https://docs.qoder.com/cli/overview.md), [Qoder overview](https://docs.qoder.com/qoder/overview) | The CLI docs put a status display below the input and document model, directory, branch, diff stats, context usage, and Vim state; custom status content may use ANSI. Lower-priority fields hide when narrow. `Ctrl+T` opens a task list, but its progress layout is unspecified. Screen-reader mode uses plain text and requires restart. The product overview describes following commands/interim results and reviewing a diff. | Use explicit field priority and width tiers; keep the live status area independent from transcript entries; connect progress to inspectable results; make accessibility output semantically complete. | Do not claim Qoder's task list, tool rows, or diff layout are documented when they are not. Keep Qoder's below-input status placement out of the scrollback stream; Agenthicc's live workspace remains AGX-11/PRD-256 territory. |
| **Grok Build** — [Getting started](https://docs.x.ai/build/overview) | The docs call the TUI fullscreen and mouse-operable and say a bottom status line displays live session context. They also document headless `-p` and `streaming-json`, without specifying a transcript schema or status-line fields. | Distinguish live context from conversation history and keep machine-readable output separate from human rendering. | Do not invent field names, progress animation, tool cards, or exact dimensions from the unspecified status line. Agenthicc does not become a fullscreen app. |
| **DeepSeek Harness** — [DeepSeek API docs](https://api-docs.deepseek.com/), [Harness quickstart](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart), [official harness repository](https://github.com/deepseek-ai/deepseek-harness), [CLI launcher README](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md) | Official material calls Harness a developer preview. The quickstart describes workspace selection, agent sessions, file/command/delegation/plan capabilities, and policy-gated approval. The `dsh` command is a launcher; `dsh web` starts a browser UI. CLI docs mention `--profile tui --resume` conditionally but do not document the terminal screen or its rendering. | Borrow only the abstract importance of a selected workspace, explicit action approval, and an inspectable plan/task boundary where Agenthicc already has equivalent contracts. | **No official DeepSeek TUI render block was found in the reviewed docs.** Do not make up or imitate DeepSeek colors, rows, controls, or a terminal transcript. Its API compatibility is not itself UI evidence. |

### 3.1 Research-to-design conclusions

1. Keep the user's transcript as a narrative, not a raw log. Use concise roles,
   operations, outcomes, and subordinate details.
2. Put the one-line explanation before the evidence: operation before arguments,
   file path/summary before bounded diff, error before diagnostic detail.
3. Borrow progressive disclosure only where it exists: PRD-255 details/overlay,
   `/history`, or another real Agenthicc interaction. Native scrollback cannot
   retroactively collapse a printed block.
4. Adapt to width by stacking and removing lower-priority metadata rather than
   squeezing several unrelated columns together. OpenCode and Qoder document
   these general behaviors; Agenthicc's exact fields remain specified here.
5. Maintain live status separately from transcript history, informed by Claude,
   Qoder, and Grok's documented separation/placement patterns. AGX-11 and PRD-256
   own Agenthicc's concrete live region.
6. Keep a plain-text semantic equivalent for every meaning conveyed by color,
   icon, indentation, or live animation.
7. Do not clone vendor brand identity. Product inspiration informs hierarchy,
   detail, and width behavior, not a copied theme.

## 4. Visual grammar

### 4.1 Three levels of attention

Every output block should have no more than three visual levels:

- **Primary:** speaker, action, changed path, failure state, or user-facing
  result.
- **Secondary:** outcome, concise summary, or directly attached supporting
  information.
- **Quiet:** time, counts, bounded previews, and implementation detail.

Primary text is readable at a glance; secondary text clarifies it; quiet text
never competes for the first fixation. Avoid borders around every event. Use
indentation and spacing for grouping, and reserve boxes for existing modal or
live components that genuinely need containment.

### 4.2 Semantic marker and color policy

Use a small, stable marker vocabulary. Current accepted examples include `❯`
for a user prompt, `●` for an assistant/tool/file heading, `└` or `⎿` for a
child row, `✓` for complete, `✗` for failure, `→` for active/requested, and
`⟳` for retry. This is a semantic role, not a mandate to place a glyph before
every row.

- Cyan: identity, operation, neutral live state, or navigation target.
- Green: successful completion or additions only, not every action heading.
- Yellow: waiting, retry, caution, or partial/unavailable usage state.
- Red: failure, rejection, deletion, or removed diff lines.
- Dim: subordinate metadata and safe diagnostics.

Every colored state also has a word or stable symbol. A green operation heading
must not imply success before the status row says `Completed`. Do not use a
full-row colored background for long conversational text; keep terminal theme
contrast and selection intact.

### 4.3 Rhythm and indentation

- Use one blank row between peer-level message/activity blocks.
- Keep headings and their child rows adjacent; never put a blank row between a
  tool heading, status and its output preview.
- Keep a diff hunk contiguous. Separate the full change block from the next
  peer-level assistant response.
- Indent a child row by two spaces or one tree continuation. Do not vary the
  child marker arbitrarily between adjacent parts of one block.
- Emit no empty line for an event that rendered no text.
- Completion recaps use PRD-257's grouped two-row form and one trailing
  separator, not this generic spacing rule.

### 4.4 Width, line fitting, and terminal safety

- Measure display-cell width, accounting for wide glyphs and ANSI sequences.
- Design review widths: 40, 48, 64, 80, 120, and 200 columns. For dense rows,
  explicitly test boundaries immediately below and above the chosen breakpoint.
- At narrow widths, remove quiet metadata first, then shorten at word/path
  boundaries. Preserve primary state, speaker/operation, and a meaningful
  outcome. Do not wrap a live status row that the owning PRD declares single
  line.
- For prose, wrap with a hanging indent under the first content cell. For code
  and diff rows, wrap only where the syntax remains unambiguous; otherwise
  truncate the preview with a visible omission marker and keep the full source
  event intact.
- ANSI-disabled output must preserve status words, grouping labels, and diff
  `+`/`-` markers. Screen-reader output must say the meaning, not merely emit
  decorative glyph names.
- Do not introduce OSC links, mouse capture, terminal mode changes, alternate
  screen behavior, or interactions not already owned by the terminal layer.

### 4.5 Content safety and fidelity

- Render user text and event-supplied strings as escaped literal content. Only
  trusted, intentionally constructed Rich renderables may carry markup.
- Keep screen-reader announcements literal and plain (BEAU-4).
- Preserve producer-side redaction and output limits. Do not add raw arguments,
  tool output, prompts, secret-shaped values, or unredacted paths to improve a
  visual example.
- Formatting is a projection only; it does not alter `ConversationEvent`,
  provider history, journal records, workflow state, usage, or replay.

## 5. Canonical cross-family render blocks

These examples describe a consistent target, not a requirement that unrelated
events be artificially joined. Bracketed color notes are descriptive and are not
literal terminal output.

### 5.1 Conversation and activity

```text
● Assistant Turn  10:41:02 UTC +0000

❯ Inspect the retry path and report any user-visible behavior change.

  → Comparing the provider recovery flow
  ✓ Checked the existing renderer tests

  ⎿ Read(src/agenthicc/tui/workspace/appender.py)
```

The per-event forms and reflow rules are specified in BEAU-2. The progress rows
are visible task copy only; they do not reveal hidden reasoning.

### 5.2 Tool and file change

```text
● Run (uv run pytest tests/unit/test_appender.py -q)
  └─ Completed · 4.2s · 37 lines
       1   42 passed
       2   0 failed
       3   duration 1.8s
       4   ...

● Update(src/agenthicc/tui/rendering.py)
└─ Added 1 line, removed 1 line

  264   # context line
  267 - old_value = _fit(label, cols)
  267 + new_value = fit(label, cols)
  ...   12 more diff lines
```

BEAU-3 defines status, preview bounds, create blocks, and width behavior.
PRD-161 defines exploratory group semantics and count limits.

### 5.3 Notices and workflow

```text
ERROR Request failed; retry budget exhausted
      Provider returned HTTP 400

⟳ Network issue · retry 2/5 in 1.0s
  Temporary network problem · HTTP 503

  ⎿ Requested: postpone phase build at the next safe boundary
  ⎿ Plan updated · phase build → queued · checkpointed
```

BEAU-4 and BEAU-5 define exact wording, status semantics, redaction, and
spacing. A request and a durable mutation are different events and must not be
made to look like one success.

### 5.4 Existing owner surfaces: pool, idle header, status, and recap

**Subagent spawn group, PRD-255 owner (scrollback):**

```text
◈ Subagents · 4 workers started
  ├ Reviewer 1  Review subagent PRDs for overlap
  ├ Reviewer 2  Check current UI rendering paths
  ├ Reviewer 3  Compare usage/status designs
  └ +1 more · Ctrl+O details
```

**Worker and pool completion, PRD-255 owner:**

```text
✓ [2/4] Reviewer 1  12.4s  Found 4 design gaps
✗ [3/4] Reviewer 2  test command failed
◈ Subagents Failed · 3/4 done · 1 unsuccessful (failed or stopped)

```

The completion rows use only `subagent_worker_done` and `subagent_pool_done`
fields: a finished worker cannot be shown as awaiting approval, and the pool
completion event does not carry a duration. The unsuccessful aggregate is kept
explicit because that event combines failed/stopped workers. Use the `Failed`
label whenever not all workers succeeded; keep the single blank row after the
pool summary before the next scrollback block.

**Live subagent panel, PRD-255/AGX-08 owner (separate from scrollback):**

```text
Subagents · 2 running · 1 needs input · 1 failed · 1/5 done · Ctrl+O details
● Reviewer 1  Reading PRDs/SUBAGENT-01…
● Reviewer 2  Comparing current renderers
◆ Reviewer 3  Needs approval: inspect test policy
✗ Reviewer 4  Could not open the requested fixture
✓ Reviewer 5  Found 4 design gaps
```

The panel is below the composer border and remains width-adaptive; it is not
copied into the scrollback transcript. Its pool summary comes from the owner’s
single pool snapshot, not from the completion-event counters.

**Idle header, PRD-256 owner; exact usage source remains PRD-157:**

```text
7ff08d4a8c5b4d6c9f0e123456789abc
/workspace/agenthicc
2 turns · cost unknown · usage partial
────────────────────────────────────────────────────────────────────────────
```

The real header has no `Session ID` or `Current directory` prefixes, preserves
identity rows, and uses a width-bounded divider. Omit lower-priority metadata
according to PRD-256; never imply unavailable values are zero.

**Live status strip, PRD-256/AGX-11 owners:**

```text
❁ Thinking · 1m 31s · ↑ 230M ↓ 1.5M · 0/4 workers
openai · deepseek-v4.1-flash
```

This strip stays in the persistent live area; it is not another `ScrollBuffer`
message and must obey AGX-11's append-safe redraw policy.

**Activity completion, PRD-257 owner:**

```text
✾ Turn · 53s
  Total · 54s
```

The total is the complete outer user activity, not model-only execution time.
It follows PRD-257's timing and grouping contract.

## 6. Complete scope and non-goals

### Goals

1. Give every registered appender event and adjacent user-visible surface an
   explicit render block, owner, and test target.
2. Make primary content more scannable than timestamps, counters, raw tool
   arguments, and diagnostic details.
3. Harmonize row rhythm and meaning while allowing specialized owners to retain
   their own layouts where those layouts serve different interactions.
4. Keep native scrollback, replay, ANSI-disabled, screen-reader, and captured
   console behavior correct.
5. Use evidence-based ideas from all six named products without fabricating
   design details or copying branding.

### Non-goals

- Altering event source, order, persistence, replay, or tool/workflow semantics.
- Replacing Rich, `ScrollBufferAppender`, `LiveRegion`, or the AGX-11 pipeline.
- Adding fullscreen/alternate-screen rendering, mouse capture, mutable
  transcript entries, search, a pager, or new UI controls.
- Replacing PRD-161/255/256/257 behavior or PRD-157 accounting.
- Rendering internal `prompt_cache` diagnostics or unknown event kinds.

## 7. Acceptance criteria

1. Every event/method in §2 has a render block in this series or a named owner
   PRD with a normative block, and no new event can silently become an
   undocumented visual kind.
2. The render blocks in BEAU-2 through BEAU-5 are normative examples: visible
   labels, indentation, order, statuses, and separators are testable; fields may
   be omitted only where the PRD declares width priority.
3. Cross-family snapshots cover a direct turn, tool exploration plus mutation,
   failing/retrying provider call, plan control, subagent pool, idle header, live
   status, completion recap, and resumed transcript.
4. The same persisted event sequence produces equivalent scrollback grouping in
   live output and replay.
5. ANSI-off and screen-reader output communicates every state in words; color
   and glyphs are supplementary.
6. Tests cover 40/48/64/80/120/200 columns, wide characters, long paths, long
   lines, empty/malformed payloads, ANSI-off, replay, and screen-reader mode.
7. No event payload, tool result, count, usage value, plan transition, worker
   state, or session record changes because of visual formatting.
8. Existing PRD-161, PRD-255, PRD-256, PRD-257, and AGX-11 tests remain green;
   their data/lifecycle contracts are not duplicated in BEAU helpers.
9. Official source links and attribution boundaries in §3 remain in the PRDs;
   claims about undocumented layout are expressly marked as unknown.

## 8. Implementation and verification plan

1. Treat this PRD as the shared visual contract. Implement helpers only within
   current Rich/appender/rendering boundaries; no new output pipeline.
2. Apply BEAU-2 to narrative/progress/mentions, BEAU-3 to tools/files, BEAU-4 to
   notices, and BEAU-5 to workflow events.
3. Review visual-only details in PRD-161/255/256/257 against the examples in
   §5. If owner PRD examples need amendments, edit those owner documents rather
   than creating duplicate behavior in BEAU.
4. Add deterministic golden outputs from real constructed events and a
   cross-family replay fixture. Keep provider/network calls mocked or absent.
5. Update only the relevant current TUI guides after rendering ships. Historical
   PRDs remain historical records.
6. Run affected TUI unit/integration/E2E tests, Ruff, format, mypy, and the
   strict documentation build. This PRD itself proposes no runtime changes.

## 9. Source limitations and refresh policy

The sources above describe product behavior, not a terminal-design standard.
Qoder's interface page specifies status and responsive behavior but not tool
cards; Codex's CLI page does not specify exact transcript grammar; Grok leaves
status fields unspecified; and DeepSeek Harness docs do not specify a terminal
renderer. Re-check these links before any later implementation that depends on
vendor-specific details. Where documentation is silent, BEAU's render blocks
are Agenthicc design decisions, not claims about another product.
