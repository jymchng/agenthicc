---
title: "TUX-08: Errors, notices and diagnostics"
status: Implemented (branch tux-tux08; the status-line display of transient notices is wired by the TUX-02 integrator)
version: 1.0.0
date: 2026-11-05
scope: "a fixed vocabulary of failure kinds for turns and providers with actionable copy; one notice component with severities; transient vs. persistent notices; stray stderr and warnings kept out of the screen; and help text free of internal identifiers"
related_prds: [TUX-00, TUX-01, TUX-02, TUX-03, TUX-06, BEAU-4, TOOLR-04, AGI-04, AGX-13]
supersedes: [BEAU-4 §4 (visual blocks)]
tags: [tui, errors, notices, diagnostics, ergonomics]
---

# TUX-08: Errors, notices and diagnostics

## Implementation status

Implemented on branch `tux-tux08`. Acceptance criteria 1-7 are covered; the table
lists each test. The amendments to the design follow it.

| AC | Test |
|---|---|
| 1 No key | `tests/unit/test_agent_turn_failure_kinds.py::test_a_missing_key_is_reported_as_missing_credentials_without_sdk_text`; `tests/e2e/test_missing_key_e2e.py` (real CLI, no key: persisted JSONL has `kind` and no SDK text; the event renders as `✗ No API key for anthropic.` and its action line) |
| 2 Every kind | `tests/unit/test_failure_kinds.py` builds each kind from a real anthropic, openai, httpx or lauren-ai exception (`test_every_kind_is_covered_by_a_real_exception`), with and without the lauren transport wrapper; unrecognised exceptions are `internal` |
| 3 Headless | `tests/e2e/test_missing_key_e2e.py` (`{"type": "error", "kind": "missing_credentials", ...}` and `error_kind` on the result line) |
| 4 One notice component | `tests/unit/test_notice.py::test_every_notice_renderer_prints_only_notices` (every notice kind in the BEAU-1 inventory prints only `Notice` objects); BEAU-4 screen-reader rules in `test_screen_reader_mode_spells_severities_and_keeps_announcements_literal` |
| 5 Transient notices | `tests/unit/test_notice_transient.py` (4 s, replaced by a newer one, no scrollback row, no conversation event) |
| 6 Stray stderr | `tests/unit/test_diagnostics.py::test_stray_stderr_reaches_the_log_and_one_notice_not_the_screen`, plus warnings, `--debug`, `/logs`, and the subprocess-fd-2 scan |
| 7 Hub help | `tests/unit/test_agents_hub.py::test_help_overlay_lists_the_keys_from_the_keymap` and `tests/unit/test_keymap_help.py` (regular expression over the rendered help of every context) |

### Amendments to the design

- **`/retry`, `/bug` and `/copy` do not exist in this repository.** The kind
  table's actions therefore say "send the message again" (and name `/model`,
  `/compact` and `/logs`, which exist); `internal` points at `/logs`, not at a
  `/bug` report (a crash reporter is a non-goal). `rate_limited` reads "Wait
  40 s, then send the message again" from the provider's `retry_after`, and
  `retried N times` counts the turn's transport retries. AC 5 is exercised
  through `/dnd` and `ConversationStore.notify`; `/copy` (TUX-05/09) calls
  `ctx.notify("success", "Copied")`.
- **The status-line display is not wired here.** TUX-02 replaced
  `StatusComponent`/`FooterComponent` with `StatusLine`/`ComposerFrame`. This
  change exposes `ConversationStore.transient_notice` (a `Signal[Notice | None]`),
  `ConversationStore.notify(severity, text)` and `CommandContext.notify`; the
  integrator renders the signal in the status line's right segment. Screen
  readers already hear it, through the `Announcer`.
- **`Notice` is a Rich renderable with `literal` and `words` flags** (BEAU-4 §4.3
  announcements stay byte-for-byte literal; `screen_reader` spells the severity
  as `Error:`, `Warning:`, `Note:`, `Done:`). The `system` event may carry a
  `severity`; it defaults to `info`. The compaction `⎋` prefix is removed from
  the texts, since the marker comes from the notice.
- **`turn_complete` and `activity_complete` keep their own rows** (PRD-257 and
  TUX-10 own them), as do the tool-row, user-message and turn-header renderers
  (TUX-06).
- **`error` payload** gains `kind`, `message` and `action` (`{"hint", "commands"}`)
  next to the existing redacted metadata. `detail` stays for text agenthicc wrote
  (the missing provider session diagnostic). The failed turn also stores the
  payload on its exception (`failure_kinds.FAILURE_ATTRIBUTE`), so workflow
  runners, checkpoints, headless JSON and background workers persist only
  agenthicc's copy (`failure_reason`), never the provider's exception text.
- **Headless** prints the `error` line only for `--headless --workflow`, the
  stream that runs turns; the `WorkflowRunCompleted` line gains `error_kind`.
- **Diagnostics.** The log is `tui.diagnostics.diagnostics_log_path(session)`;
  it is installed right after the first frame (so the first-frame module budget
  is unchanged) and handed to `Workspace.redirect_stderr`, which survives an
  external-editor handoff. `--debug` is a new global flag (`CLIFlags.debug`).
  `/logs` opens a small pager overlay (`LogTailOverlay`) until TUX-09's
  transcript view takes it over. The "no subprocess inherits fd 2" test is a
  scan of the source for spawns without a `stderr` or `capture_output`
  argument; the external editor, which owns the terminal after the live block
  is suspended, and CLI-only commands are listed exceptions.
- **Help phrases.** An unknown condition still reads as words ("when future
  flag") rather than failing the help screen; `test_every_condition_in_the_default_actions_has_a_phrase`
  fails instead.
- **Command messages.** Every usage and error print in `commands/builtins.py` goes
  through `ctx.notify`; `builtins.py` has no colour literals and is removed from
  `test_theme_tokens._PENDING`. Tables and listings keep `console.print`.

## 1. Problem

From TUX-00: F19 and F21. See `screens/agt-error.png`.

### 1.1 A missing key is reported as a type name

- With no API key, the Anthropic SDK raises `TypeError("Could not resolve authentication method. Expected either api_key or auth_token to be set…")`.
- `AgentTurn` reports `Agent turn failed (TypeError)` (`runners/agent_turn.py:3146`).
- The renderer prints `ERROR Agent turn failed (TypeError)` in bold red (`tui/workspace/appender.py:781`).

### 1.2 The redaction is right, but nothing replaces the text

- `_provider_failure_metadata` (`agent_turn.py:279`) deliberately persists no exception text, because provider messages can embed prompts or secrets.
- That is correct, but nothing turns the exception into a *kind* the user can act on. Only HTTP failures get a status: `Provider request failed (HTTP 401)` tells the user nothing about which key, profile or command to fix.

### 1.3 Notices have no common look

Notices are styled one by one:

- `error`: red bold `ERROR`;
- `mcp_server_failure`: red or yellow text;
- `tool_recovery`: dim;
- `network_retry`: its own format;
- footer notifications;
- `ctx.console.print("[red]Usage:[/red] …")` in commands (`commands/builtins.py:1434`, among many).

There is no single severity scale, and no difference between "this happened" (persistent, in the scrollback) and "FYI" (transient, in the status line).

### 1.4 Stray output can reach the screen

- `LiveRegion` proxies `sys.stderr` into the console (`tui/workspace/live_region.py:231`). Any library that writes to stderr, or `warnings.warn`, prints into the transcript between frames.
- Codex shows the cost of this: its own `ERROR rmcp…` log lines are printed over its UI (`screens/cx-idle.png`).

### 1.5 Internal names in help

- The agents hub's `?` help prints keymap condition expressions next to each key: `(library_tab, not needs_approval)` and `(editable_definition, not session)` (`tui/workspace/agents_hub/render.py:424`).
- Users see internal flag names.

## 2. Goals

### 2.1 Failure kinds

`runners/failure_kinds.py: classify_turn_failure(exc, provider) -> FailureKind`.

- It is a pure function over the exception's type, HTTP status, and provider error code.
- Where the SDK offers no type or code, it matches a **fixed allow-list of SDK message prefixes**, tested per provider SDK version.
- The matched text is never stored: only the kind is.

| Kind | Detected from | Message (provider, model, profile substituted) | Action offered |
|---|---|---|---|
| `missing_credentials` | auth-resolution errors before any request; no key in env or profile | "No API key for anthropic." | "Set ANTHROPIC_API_KEY, or run /model to switch provider." |
| `rejected_credentials` | HTTP 401/403, `authentication_error`, `permission_error` | "anthropic rejected the API key (profile work)." | "Check the key, or /model." |
| `rate_limited` | 429, `rate_limit_error` | "Rate limited by anthropic; retried 3 times." | "/retry, or wait. The next retry is in 40 s." |
| `overloaded` | 529, 503, `overloaded_error` | "anthropic is overloaded." | "/retry" |
| `context_too_long` | 400 with prompt-too-long codes | "The conversation is longer than opus-4.8's context." | "/compact, then /retry" |
| `model_not_found` | 404 on the model, `not_found_error` | "anthropic has no model named claude-x." | "/model" |
| `billing` | 402, `insufficient_quota`, credit errors | "anthropic refused the request for billing reasons." | "Check the account." |
| `network` | connection, DNS or TLS errors | "Cannot reach api.anthropic.com." | "Check the network or proxy, then /retry." |
| `timeout` | request timeout | "anthropic did not answer in 600 s." | "/retry" |
| `cancelled` | user interrupt | (no error; TUX-10's "interrupted" row) | — |
| `internal` | anything else | "The turn failed unexpectedly (TypeError)." | "/bug writes a report; the log is at ~/.agenthicc/logs/…" |

How the kinds are used:

- **The event.** `error` events carry `kind` and a structured `action`, alongside the existing redacted metadata.
- **Detail.** The `detail` field is reserved for text agenthicc wrote itself.
- **Retries.** `/retry` exists today through AGI-04's retry. The table only names it.
- **Other surfaces.** The same kinds are shown by the background hub's failed rows (AGX-07) and by headless JSON (`error.kind`).
- **Pre-flight.** TUX-03's pre-flight check reports `missing_credentials` before the first turn. This table covers the case where it still happens, for example when a key is removed during the session.

### 2.2 One notice component

`tui/workspace/notice.py: Notice(severity, title, body, actions)`, rendered with TUX-01 tokens and glyphs:

```text
 ✗ No API key for anthropic.
   Set ANTHROPIC_API_KEY, or run /model to switch provider.

 ! MCP server "github" is unavailable; continuing without its 14 tools.   /mcp

 · Compacted 84 messages into 1 summary (saved 61k tokens).
```

**Severities:**

- `error`: `danger` ✗;
- `warning`: `warning` !;
- `info`: `muted` ·;
- `success`: `success` ✓, used only for user-requested results such as "Saved theme".

**Placement.**

- **Persistent** notices go into the scrollback. They are for things that happened in the conversation: turn failures, MCP failures, compaction, recovery, model change.
- **Transient** notices replace the status line's right segment for 4 s (TUX-02). They are for confirmations and FYIs: "Copied", "Mode → Plan", "Queued". They never enter the scrollback.

**Migration.**

- Every renderer listed in 1.3 renders through `Notice`.
- Every command's direct `console.print` of a usage or error message goes through a `ctx.notify(severity, …)` helper.
- A lint test flags `console.print("[red]` and `[yellow]` patterns in `commands/` and `tui/`. With TUX-01's token lint, these become impossible anyway.

### 2.3 Nothing stray on the screen

- During the TUI, the `sys.stderr` proxy writes to the session log file (`~/.agenthicc/logs/<session>.log`), not to the console.
- `warnings` are captured into the log.
- The first stray write of a session shows one transient notice: "Diagnostics were written to the log · /logs".
- `/logs` opens the log tail in TUX-09's transcript view.
- Child processes already capture stderr (MCP `stderr_tail`, `tools/exec`). A test ensures no subprocess started by the session inherits fd 2 while the TUI is active.
- `--debug` keeps the current behaviour (stderr to the screen) for developers.

### 2.4 Help without internal identifiers

- `HelpEntry.conditions` (`tui/keymap.py:232`) is rendered through a phrase table.

  | Condition | Phrase |
  |---|---|
  | `library_tab` | "in Library" |
  | `needs_approval` | "when an agent needs approval" |
  | `session` | "on a session" |
  | `editable_definition` | "on your own agent definitions" |
  | `running` | "while a turn runs" |
  | … | (every condition in `DEFAULT_ACTIONS`) |

- A negated condition reads "except …".
- An unknown condition fails a unit test, not the help screen.
- The hub's help and `/shortcuts` (TUX-10) both use the phrases.

## 3. Non-goals

- Changing the redaction policy. No exception text from a provider is ever shown or persisted.
- Retry policy (TUIR and AGI-04 own it).
- A crash reporter. `/bug` writes a local report; it does not upload one.

## 4. Acceptance criteria

1. **No key.** With no key, a turn renders `✗ No API key for anthropic.` and the action line. The persisted `error` event has `kind = "missing_credentials"` and contains no SDK message text (asserted on the JSONL).
2. **Every kind.** For each kind in 2.1, a unit test builds the provider SDK's real exception (anthropic and openai SDKs, `httpx` errors) and asserts the kind. Unrecognised exceptions are `internal`.
3. **Headless.** `agenthicc --headless` emits `{"type": "error", "kind": "missing_credentials", …}` for the same case.
4. **One notice component.** Every error, warning and info line in the scrollback renderers comes from `Notice`, checked by the renderer coverage test from BEAU-1.
5. **Transient notices.** `/copy` shows "Copied" in the status line for 4 s, and the scrollback gains no row.
6. **Stray stderr.** `print("boom", file=sys.stderr)` inside a tool during a TUI session adds nothing to the captured screen, writes `boom` to the session log, and shows the one-time notice.
7. **Hub help.** The hub's `?` overlay contains no `_` identifier and no `not ` condition text. The test scans the rendered text with a regular expression.

## 5. Rollout

- `error` event payloads gain `kind` and `action`, documented in `llms-full.txt`.
- Old logs without `kind` render as `internal`, with their stored message.
- BEAU-4 is marked "Superseded in part by TUX-08 (visual blocks)", and its accessibility rules for announcements still apply.

## 6. Verification

- `uv run pytest tests/unit/test_failure_kinds*.py tests/unit/test_notice*.py tests/unit/test_keymap_help*.py tests/e2e/test_missing_key*.py -q`
- The redaction tests in `tests/unit/test_agent_turn*` pass unchanged.
