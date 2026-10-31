---
title: "BEAU-2: Conversation Narrative, Progress, and Mention Blocks"
status: Proposed
version: 2.0.0
created: 2026-10-31
scope: "specify exact visual blocks for turn_start, user_message, text, thinking_step, and mention_chips"
related_prds: [BEAU-1, AGX-11, PRD-167, PRD-201]
tags: [tui, scrollback, conversation, mentions, accessibility]
---

# BEAU-2 — Conversation Narrative, Progress, and Mention Blocks

## 1. Problem and objective

`turn_start`, `user_message`, `text`, `thinking_step`, and `mention_chips` are
the narrative spine of scrollback. The appender currently prints a turn label
and timestamp, a highlighted user prompt, assistant Markdown, dim progress, and
one row per resolved mention. Their distinct roles need one consistent visual
hierarchy so the transcript reads as conversation with subordinate activity,
not as unrelated logs.

This PRD specifies exact blocks and width behavior while preserving event
order, payloads, Markdown semantics, replay, and the separation between
scrollback and the live workspace.

## 2. Research applied

- **Claude Code** documents visible transcript messages, transcript navigation,
  `/focus`, a classic renderer that preserves native scrollback, and expandable
  tool details in fullscreen mode. Borrow clear narrative hierarchy and the
  distinction between conversation and tool details. Do not promise click
  expansion, fullscreen search, or in-place update in append-only scrollback.
- **Codex CLI** encourages inspecting commands and diffs as work appears but
  does not specify exact transcript typography. Keep progress and tool records
  in event order; do not attribute a row style to Codex.
- **Qoder CLI** documents plain-text screen-reader output and hiding lower
  priority status fields when narrow. Qoder's overview emphasizes following
  intermediate results and reviewing diffs. Borrow explicit semantic hierarchy
  and width priority, not a copied statusline.
- **OpenCode** provides a tool-detail toggle, but does not document message
  styling. Do not hide conversation content behind a new toggle.
- **Grok Build** documents a separate live session-context line. Keep live
  status out of message content; PRD-256/AGX-11 own that surface.
- **DeepSeek Harness** docs do not specify terminal transcript rendering; no
  DeepSeek-specific message style is inferred. See source matrix in BEAU-1.

Official sources: [Claude Code fullscreen](https://code.claude.com/docs/en/fullscreen),
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli),
[Qoder CLI interface](https://docs.qoder.com/cli/interface.md),
[Qoder overview](https://docs.qoder.com/qoder/overview),
[OpenCode TUI](https://opencode.ai/docs/tui/),
[Grok Build](https://docs.x.ai/build/overview), and
[DeepSeek Harness guide](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart).

## 3. Event contract and normative render blocks

Examples show text output; color notes are descriptive, not literal. Angle
brackets indicate example event values.

### 3.1 `turn_start` — assistant turn anchor

```text
● Assistant Turn  10:41:02 UTC +0000
```

```text
● Reviewer  10:41:02 UTC +0000
```

Use `Assistant Turn` for the default `assistant` label; otherwise retain the
existing agent label. Include the local timezone abbreviation and numeric UTC
offset in the timestamp. Do not add model, session, workflow, or status metadata
not present in this event. Escape the agent label before Rich rendering. One
anchor opens the turn; `text` events do not repeat it. Normally place one blank
row after the heading. Defer that separator until the next visible event; when
`mention_chips` follows, render its resolved read rows directly beneath the
heading and place the blank row after the complete mention group. Never wrap the
heading: shorten only low-priority label suffixes when needed.

### 3.2 `user_message` — prompt block

```text
❯ Please trace the provider retry path and summarize the user-visible behavior.
  Keep the explanation short and include the relevant test names.
```

Keep `❯` as the user marker; do not add an invented `You:` or timestamp to
every prompt. Wrap continuation text under the first message character. Preserve
intentional newlines, paragraph gaps, lists, quotes, and code fences. Render
payload as literal text, not Rich markup; reject terminal control effects.
Do not use a full-row background on long messages; retain selection and terminal
theme contrast. Place one blank row after a non-empty prompt block, not after an
empty event.

### 3.3 `text` — assistant Markdown block

```text
The retry budget is **five retries** after the initial request. A permanent
provider rejection is surfaced after the budget is exhausted.

The recovery tests cover the boundary and resume behavior.
```

Keep the turn anchor as the speaker identity. Preserve current Rich Markdown
semantics for emphasis, lists, quotes, links, tables, and fenced code. Wrap prose
by display cells; do not reflow source code or alter syntax. Multiple `text`
events stay in event order; do not create a new joining state machine. Empty
text remains silent. Preserve the current separator after non-empty Markdown,
except where an existing owner PRD groups the following completion row.

### 3.4 `thinking_step` — visible task progress, not hidden reasoning

```text
  → Inspecting provider recovery
  ✓ Compared the retry tests
```

Show only `step` and `done` from the event. Do not expose hidden model
reasoning, private planning, tool arguments, or synthesized internal thoughts.
Use `→` for active and `✓` for completed, with screen-reader words
“In progress” and “Completed”. Keep steps subordinate but readable, wrap with
the continuation aligned beneath the text, escape payload strings, and never
rewrite an earlier scrollback row.

### 3.5 `mention_chips` — resolved reference rows

```text
  ⎿ Read(src/agenthicc/tui/workspace/appender.py)
  ⎿ Listed(src/agenthicc/tui/workspace)
  ⎿ Fetched(https://example.org/docs)
  ⎿ ✗ Read(src/missing.py)
```

Keep one child row per chip, in event order. Use the existing verb mapping:
file/glob → `Read`, directory → `Listed`, URL → `Fetched`. Strip only the
leading `@` used for mention syntax and escape the remaining target. Preserve
failure state explicitly; never imply success from a colored path. Long paths
wrap with a hanging indent and remain copyable. Do not duplicate the user prompt
or synthesize an aggregate result. When this event immediately follows
`turn_start`, keep the mention rows attached to the heading with no intervening
blank row; separate the complete mention group from later content with one blank
row.

## 4. Combined sequence and spacing

This is a composition example, not a mandate to reorder producer events:

```text
❯ Find why provider recovery stops after an HTTP 400.

● Assistant Turn  10:41:02 UTC +0000
  ⎿ Read(src/agenthicc/runners/agent_turn.py)
  ⎿ Read(tests/unit/test_provider_recovery.py)

  ✓ Located the bounded-retry handler
  → Comparing the permanent-error tests

The retry loop gives permanent provider errors a separate bounded recovery
budget. The final failure keeps the checkpoint paused at the active phase.
```

Peer blocks have one blank row. The prompt renderer retains its trailing blank;
mention rows remain adjacent to each other and, when they immediately follow a
turn heading, directly beneath it. Defer the heading separator until the next
visible event, then place one blank row after the complete mention group before
later content. Progress rows likewise remain adjacent. Preserve event order;
tool and file events use BEAU-3. Do not force unrelated events into a card.

## 5. Responsive and accessibility contract

Use terminal display-cell measurement, including wide glyphs and ANSI:

| Width | Required behavior |
|---:|---|
| 120+ | Keep full labels, timestamp, prose, and paths. |
| 80–119 | Preserve narrative; shorten only long role-label suffixes. |
| 48–79 | Drop low-priority heading metadata before narrative content. |
| 40–47 | Keep marker and leading text; wrap with hanging indentation. |

Conversation prose may wrap. A turn heading must remain recognizable; code
must remain code. Never use Qoder's status-field removal rule to discard message
content. ANSI-disabled output retains labels and tree markers. Screen-reader
output says `Assistant turn at …`, `User: …`, `In progress: …`, `Completed: …`,
and mention action/result in words. Do not capture mouse, switch alternate
screens, or change native scrollback behavior.

## 6. Non-goals

- Changing event generation, persistence, Markdown parsing, mention resolution,
  redaction, provider history, or turn semantics.
- Adding a fullscreen transcript, interactive collapse/search, mutable entries,
  or hidden-reasoning display.
- Replacing tool, notice, workflow, status, or completion renderers.

## 7. Acceptance criteria

1. Each event kind above has snapshots matching §3; labels, order, indentation,
   and separators are asserted independently of ANSI color.
2. A turn has one speaker anchor, not one heading per `text` event.
3. Text, Markdown, code, paths, and mentions are faithful and escaped, including
   long strings and wide Unicode.
4. Progress reveals only event-provided visible step text; active/completed state
   is clear in plain and screen-reader output.
5. Mention rows preserve kind, success/failure, and event order without invented
   results.
6. Test widths 40, 48, 64, 80, 120, and 200 in ANSI-on/off and screen-reader
   modes; test empty/malformed fields, multiline Markdown, fenced code, URLs,
   long paths, and replay.
7. Live and replay block boundaries match; formatting changes no event payload.

## 8. Implementation and verification

Use existing appender and Rich rendering boundaries and cell-width helpers.
Extend appender/TUI snapshot tests for normal and malformed events, replay,
ANSI-off, screen reader, and captured output. No live provider calls are needed.
Update current TUI documentation after implementation is verified.
