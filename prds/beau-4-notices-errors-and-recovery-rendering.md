---
title: "BEAU-4: Notices, Errors, Recovery, and Accessible Announcements"
status: Proposed
version: 2.0.0
created: 2026-10-31
scope: "specify render blocks and urgency hierarchy for system/error/recovery/MCP/retry/announcement events"
related_prds: [BEAU-1, PRD-157, PRD-191, PRD-198, AGX-11, AGX-13]
tags: [tui, scrollback, diagnostics, accessibility, recovery]
---

# BEAU-4 — Notices, Errors, Recovery, and Accessible Announcements

## 1. Problem and objective

The appender renders operational notices from seven event kinds: `error`,
`system`, `announcement`, `tool_recovery`, `mcp_server_failure`,
`network_retry`, and `provider_recovery_retry`. They do not all mean the same
thing. A failure needs attention, a retry is ongoing work, an optional MCP
startup failure may be non-blocking, a system notice may be informational, and
an accessibility announcement is required to remain literal plain text.

This PRD defines distinct blocks for each, with urgency conveyed by words
before color. It preserves bounded/redacted producer fields and does not turn
notices into a competing notification system.

## 2. Research applied

- **OpenCode** documents attention events for questions, permission requests,
  errors, and completed sessions, and has configurable notifications. Borrow
  clear semantic distinction between attention-worthy and ordinary output; do
  not add a new sound/notification system.
- **Claude Code** supports transcript review/search and collapsible tool output
  in fullscreen, while classic output remains native scrollback and is used for
  screen-reader mode. Borrow readable transcript order; no modal/tool-detail
  interaction is added here.
- **Qoder CLI** documents plain-text screen-reader output and screen-width
  prioritization. Preserve explicit words, safe wrapping, and literal
  announcements.
- **Codex CLI** documents permission settings/status but no exact warning or
  error row style. Keep approvals/errors textually distinct without claiming
  Codex styling.
- **Grok Build** separates live session context from transcript and documents
  headless structured output. Keep transient live context and human notices
  distinct; do not reuse these visual blocks for machine-readable output.
- **DeepSeek Harness** documents policy-gated action approval but no TUI notice
  rendering. The design here is Agenthicc-specific; no DeepSeek layout is
  inferred.

Sources: [OpenCode TUI](https://opencode.ai/docs/tui/),
[Claude Code fullscreen](https://code.claude.com/docs/en/fullscreen),
[Qoder CLI interface](https://docs.qoder.com/cli/interface.md),
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli),
[Grok Build](https://docs.x.ai/build/overview), and
[DeepSeek Harness quickstart](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart).

## 3. Shared notice block contract

### 3.1 Priority

Every notice must answer, in this order:

1. What happened or is happening?
2. Is the session blocked, retrying, or continuing?
3. What short reason/detail is needed to understand it?

Use a concise first line and at most one or two subordinate lines. Keep
implementation detail quiet. Do not surround a one-line notice with a box.
Do not collapse the final error into repeated retry chatter.

### 3.2 Semantics and color

| Meaning | Required text | Optional cue |
|---|---|---|
| Information | Existing readable message or `Info` only when no better label exists. | Dim/neutral. |
| Retry in progress | `Network issue` / `Provider request rejected` plus attempt and delay. | Yellow `⟳` or `⚠`. |
| Optional dependency failure | Existing message must communicate continuing without the dependency when payload says optional. | Yellow. |
| Required dependency failure | Existing message plus failure severity. | Red. |
| Error | `ERROR` and safe message. | Red. |
| Accessible announcement | Literal event text, unmodified. | None required. |

Color is supplemental. Do not make system text red just to create visual variety,
or make an operation look successful because its heading is green.

## 4. Normative render blocks

### 4.1 `error`

```text
ERROR Provider request failed after the recovery budget was exhausted
      The provider returned HTTP 400
```

- Keep `ERROR` as the explicit severity label and the event's safe `message`
  as the primary text.
- Render `detail` on a subordinate line only when non-empty; preserve the
  current 500-character maximum unless its owner changes the event contract.
- Do not add a stack trace, provider request body, raw exception repr, or
  inferred cause. Escape both strings as literal content.
- Wrap the body below its first character at narrow widths; keep `ERROR` and
  the first part of the message together. Do not silently drop the message.

### 4.2 `system`

```text
Context was compacted. Earlier conversation remains available in history.
```

The example is illustrative; render the actual event `text`. Keep generic system
messages neutral/dim and separated from the next peer block by one blank row,
as today. Do not prepend `SYSTEM` to every line if the message is already
self-describing. Do not upgrade severity unless an event field expressly says
so. Escape text; an empty event produces no block.

### 4.3 `announcement`

```text
The approval request is ready. Use the approval controls to continue.
```

This row must be the event's exact literal text: no `INFO` prefix, glyph, color,
Markdown parsing, paraphrase, truncation, or Rich markup. Preserve its event
order. Do not also emit a duplicate decorated status. In screen-reader tests,
confirm it is announced once and remains useful in plain terminal capture.

### 4.4 `tool_recovery`

```text
Tool execution was interrupted; incomplete tool results were recorded so the
session can continue safely.
```

Use the already-bounded, redacted `text`, or the current safe default when the
field is absent. A quiet recovery glyph/label may be added only if the wording
remains the primary meaning. Keep it distinct from a normal tool failure and
from a terminal session crash. No stack/transaction internals are displayed.

### 4.5 `mcp_server_failure`

Example using the current safe default event text:

```text
MCP server is unavailable; continuing without its tools.
```

`required == true` changes the existing severity style to red; optional
failure uses yellow. The event-provided text remains authoritative in either
case. Do not invent a stronger required-server consequence in the renderer or
synthesize a server name, endpoint, credential, or configuration detail. Keep
one blank separator after the block. If the event text does not explain a
required/optional consequence users need, update and test the safe producer
contract before changing this presentation.

### 4.6 `network_retry`

```text
⟳ Network issue · retry 2/5 in 1.0s
  Temporary network problem · HTTP 503
  The connection was interrupted; retrying the request.
```

- First line: event attempt, max retries, and delay; use exactly the provided
  bounded values, clamped by current rendering rules.
- Second line: reason and HTTP status when present. Use `Retrying…` if no
  metadata is available.
- Third line: detail only when present and not a duplicate of the reason.
- Never display nested SDK dictionaries, a traceback, or provider-sensitive
  exception representation. Preserve current redaction and order.
- A retry is not a completed operation. Use explicit `retry` language.

### 4.7 `provider_recovery_retry`

```text
⚠ Provider request rejected · retry 2/5 in 1.0s
  HTTP 400
  The provider rejected the request format; attempting bounded recovery.
```

Use the current fallback `provider rejected request` if no numeric status is
provided. Keep `detail` subordinate and bounded by the producer contract. Do
not conflate this request-repair/recovery retry with a transient network retry;
the headings must make the two families distinguishable in a long transcript.

## 5. Width and grouping behavior

- Use display-cell-aware wrapping at 40/48/64/80/120/200 columns.
- Keep the severity/retry label and the first meaningful message phrase on the
  first row whenever possible. Wrap diagnostics below the message, not beside
  the status icon.
- At narrow widths, remove duplicate/quiet detail first. Do not remove the
  final failure, retry attempt, required/optional consequence, or announcement.
- Repeated retries remain separate canonical events in order. If a later final
  failure occurs, the final `error` must receive its own conspicuous block.
- Do not group notices across a user/assistant message, turn, workflow boundary,
  approval, or unrelated tool event. No mutable notification card is allowed.
- ANSI-disabled and screen-reader output uses words. Do not rely on glyph
  distinctions like `⟳` versus `⚠` alone.

## 6. Security, ownership, and limits

- Reuse producers' bounded, redacted fields. The UI must not inspect provider
  request bodies or reconstruct raw exception strings.
- Escape untrusted message/detail text before Rich markup.
- Do not render `prompt_cache` or other unregistered internal diagnostics.
- Do not alter retry budgets, error classification, MCP lifecycle, event order,
  session state, approvals, or accessibility event semantics.
- Keep provider-recovery semantics under PRD-198 and recovery/session semantics
  under PRD-191; this is presentation only.

## 7. Research-to-decision traceability

| Evidence | Applied decision |
|---|---|
| OpenCode identifies questions, permission requests, errors, and completed sessions as attention events. | Distinguish urgency in transcript wording; do not transform all notices into one severity. |
| Claude Code provides expandable tool details only in its fullscreen renderer and has a classic accessible/native-scrollback path. | Notice blocks are readable without expansion and do not require a special renderer. |
| Qoder's screen-reader mode is plain text. | Every notice state and outcome is spoken as words. |
| Codex exposes permissions/status but not an exact notice design. | Keep policy state explicit without borrowing unverified visual styling. |
| Grok separates live session context and documents structured headless output. | Do not mix live status or JSON machine output into these human notice blocks. |
| DeepSeek Harness source docs do not specify terminal notice layout. | No DeepSeek rendering claim is made. |

## 8. Non-goals

- Changing retry/backoff, failure classification, MCP lifecycle, tool recovery,
  or screen-reader event emission.
- Adding a toast system, notification inbox, sound, prompt, or second
  announcement pipeline.
- Displaying secrets, internal stack traces, hidden prompts, or unregistered
  diagnostics.

## 9. Acceptance criteria

1. All seven event kinds have snapshots matching §4, with exact state words and
   grouping asserted separately from color.
2. Information, warning, retrying, required failure, optional failure, and fatal
   error are distinguishable in plain text.
3. `announcement` is byte-for-byte literal at its event text boundary and is not
   decorated or duplicated.
4. Retry events preserve attempt/delay/reason ordering; a final error is not
   swallowed by retry output.
5. MCP optional failure does not imply fatal session failure; required state
   is only elevated when event semantics establish it.
6. Malformed/missing fields use safe defaults and never leak secrets or
   traceback/SDK payloads.
7. Test 40/48/64/80/120/200 columns, ANSI-off, screen-reader, replay, long
   detail, duplicate detail, empty notice, and retry-to-final-error sequences.

## 10. Verification

Add focused appender snapshots and replay tests for each notice, including
ANSI-disabled and announcement capture. Review the current event producers
before changing user-visible copy. No live provider/MCP server is required.
