---
title: "TUX-06: Transcript density and tool rows"
status: Proposed
version: 1.0.0
date: 2026-11-05
scope: "how the scrollback shows turns, user messages, tool calls, commands, output, memory reads and missing mentions: one-line tool rows, human-readable commands, quiet success, structured (markup-free) tool payloads, a detail toggle, compact headers and faithful replay timestamps"
related_prds: [TUX-00, TUX-01, TUX-09, BEAU-1, BEAU-2, BEAU-3, PRD-161, AGX-11, TOOLR-04]
supersedes: [BEAU-2 §3–§4 (render blocks and spacing), BEAU-3 §4 (render blocks)]
tags: [tui, transcript, scrollback, tools, density]
---

# TUX-06: Transcript density and tool rows

## 1. Problem

From TUX-00: F15, F16, F17, F18 and F20. See the replay in `screens/agt-tools.png`.

### 1.1 Every tool call is 3–7 rows

`ScrollBufferAppender._render_tool_complete` (`tui/workspace/appender.py:491–556`) prints:

- `● Operation(args)`;
- `└─ Completed  N lines`;
- **four** numbered output lines;
- `⋯ +N more lines`;
- a blank line.

A turn that reads ten files therefore fills a screen with file heads the user did not ask to see. "Completed" in green is printed after every success, which is the normal case.

### 1.2 Arguments are a Python repr

`runners/agent_turn.py:664` `_fmt_args` formats a single argument as `repr(value)[:60]`. This has three effects:

- `run_bash` with `cd /root/youtube_projects/youtube-shorts/objective-create-a && npm test` shows `Run('cd /root/youtube_projects/youtube-shorts/objective-create-a)`, with the quote left unbalanced. **The command that matters is cut off.**
- Paths are quoted, and long ones lose their filename end.
- Memory reads print raw JSON.

### 1.3 Presentation is persisted

- The `tool_complete` payload stores **Rich markup**:
  - `args_str` = `"[dim](…)[/dim]"`;
  - `dur_str` = `"  [dim]4ms[/dim]"` (`agent_turn.py:1378–1379`).
- The event log therefore holds styling, and the hub's detail view has to strip it (`agents_hub/detail.py:122`).
- A theme change (TUX-01) cannot restyle old events.

### 1.4 Headers and timestamps

- Turn headers read `● assistant (deepseek-v4.1-flash)  05:04:03 CET +0100` (`_hhmmss`, `appender.py:135`, uses `%H:%M:%S %Z %z`). Every turn repeats the time zone.
- In the study, `/replay` of an older session showed the replay's wall-clock time on each turn rather than the original time.

### 1.5 Missing mentions

An `@` mention of a missing path renders as a failed tool row, `✗ Read(...)`. To the user it looks as if the agent failed, when in fact the input was wrong.

### 1.6 Earlier specifications

BEAU-2 and BEAU-3 specify these blocks, and they keep the four-line preview and the mandatory "Completed". They were never implemented. This PRD replaces their visual blocks. BEAU-1's renderer inventory and coverage rules stay.

## 2. Goals

### 2.1 Structured tool payloads (data, not markup)

`tool_complete` carries:

| Field | Type | Content |
|---|---|---|
| `summary` | `str` | the human target: the command, path or query (2.2) |
| `args` | `dict[str, str \| int \| float \| bool \| None]` | a bounded, redacted projection: each value ≤ 512 characters, at most 8 keys |
| `duration_ms` | `int \| None` | the duration in milliseconds |
| `output_lines` | `list[str]` | unchanged, bounded |
| `output_total` | `int` | the total number of output lines |
| `exit_code`, `error_code` | `int \| None`, `str \| None` | from TOOLR-04's envelope |

Changes:

- `args_str` and `dur_str` are deleted.
- The appender formats everything at render time with TUX-01 tokens.
- The hub's `detail.py` stops stripping markup.
- Older logs that hold `args_str` and `dur_str` are read by a one-way migration in the session-log loader. It strips the markup into `summary` and parses the milliseconds. There is no dual rendering path.

### 2.2 Human-readable summaries per tool

Each built-in tool family registers a `summarize(args) -> str` beside its `_TOOL_OP` operation name:

| Tool | Row |
|---|---|
| `run_bash`, `run_command` | `$ npm test` — the **last** command of a `cd … &&` chain, with `in objective-create-a/` as muted detail; multi-line scripts show the first line and `+N lines` |
| `read_file`, `batch_read` | `src/app/main.py:40-120` or `3 files` |
| `write_file`, `patch_file`, `apply_diff` | `src/app/main.py +12 −3` (counts from the diff) |
| `grep_files`, `search_files` | `"TODO" in src/ → 14 matches` |
| `list_directory` | `src/agenthicc/ → 23 entries` |
| `git_*` | `git diff --stat` style: `git status → 3 changed` |
| memory reads | `memory: "deploy notes" (3 entries)`, never raw JSON |
| web/fetch | the host and path, `docs.python.org/3/library/asyncio` |
| MCP and plugin tools | `server · tool` and the first string argument, bounded |

- Paths are made relative to the workspace and middle-truncated (`src/…/appender.py`), so the filename always shows.
- A plugin tool may supply its own `summarize` through the documented tool metadata. Otherwise the first string argument is used.

### 2.3 One row per tool by default

```text
 ● Read src/agenthicc/tui/workspace/appender.py:480-560
 ● $ uv run pytest tests/unit -q                                   2.4s
   └ 1,284 passed, 3 skipped
 ✗ $ npm test  in objective-create-a/                     exit 1 · 8.1s
   │ FAIL src/App.test.tsx
   │   ● renders learn react link
   │     Unable to find an element with the text: /learn react/i
   └ 41 more lines · ctrl+t to expand
 ● Edit src/app/main.py  +12 −3
```

- **Success is quiet.**
  - One row: the bullet in `muted`, the operation in `text`, the summary.
  - The duration is right-aligned in `muted`, only when it is ≥ 1 s.
  - There is no "Completed" and no output.
- **Commands** (execute capability) add **one** result row: the last non-empty output line, or a parsed test summary when one is recognised (pytest, jest/vitest, cargo, go test).
- **Failures** expand by default:
  - the `danger` ✗;
  - the exit code or error code;
  - up to 8 output lines from the **end** of the output, where errors are;
  - a `ctrl+t to expand` footer.
- **Running and waiting states** keep their words (`running`, `ready`, `waiting`, `timed out`, `cancelled`), as BEAU-3 §4.2 requires. Only plain success loses its word.
- **File edits** keep BEAU-3's diff block, with TUX-01 diff tokens, capped at 20 lines unless expanded.
- **Exploration groups** (PRD-161) collapse to one row: `● Explored 9 files, 3 searches` with the paths as muted detail. The individual rows appear on expand.
- **No blank line between consecutive tool rows.** One blank line comes before the next assistant text.

### 2.4 The detail level

- `[ui] tool_detail = "compact" | "normal" | "verbose"`. The default is `compact`, as above.
  - `normal` adds the four-line head preview for successful reads and commands.
  - `verbose` prints the full bounded output.
- `/details` cycles the level for the session and re-renders nothing that is already printed. That way the scrollback stays an append-only log (AGX-11), as in OpenCode.
- Ctrl+T (TUX-09) opens the last tool's full output in the transcript view.

### 2.5 Turn headers and user messages

- **The user message.**
  - It is a `user_bg` band (TUX-01) with the `accent` chevron, and wraps with a hanging indent.
  - Pastes show their condensed placeholder (`[pasted 120 lines]`).
  - Mentions show as chips (`@src/app.py`).
- **Assistant turns have no header row** when the agent is the default and the model has not changed since the previous turn. The text simply follows the user message.
- **A header is printed** only when the agent or model changes, or after a gap of more than 30 minutes: `◆ reviewer · sonnet-4.6 · 14:02`.
- **Times are `HH:MM` local.** The time zone appears only in `/status` and the transcript view's detail pane.

### 2.6 Replay keeps the original times

- `/replay` and resume render every event with its stored `timestamp`. A separator shows where the past ends: `── replayed from 2 Nov 05:04 · 827 events ──`, and `── now ──` after it.
- A test replays a fixture whose events are dated 2026-01-01, and checks that no rendered time is the current time.

### 2.7 Mentions that fail are input notices

- A mention that cannot be resolved (missing, outside the workspace, too large) is rendered under the user message as a `warning` chip with the reason: `@docs/missing.md not found`.
- It is not a failed tool row.
- The model still receives the existing unresolved-mention note.

## 3. Non-goals

- Changing what is persisted for kernel replay, other than the presentation fields named in 2.1.
- Markdown rendering of assistant text.
- The full-screen transcript view (TUX-09).

## 4. Acceptance criteria

1. **Density.** Replaying the `agt-tools` fixture session (10 tool calls, 2 assistant texts) at 120 columns produces no more than 40 % of the rows the current renderer produces for the same events in `compact` mode. The baseline is recorded by the same recording-console test before the change.
2. **No markup in events.** No `tool_complete` event written by a new session contains `[` + a style name + `]`. A test runs a scripted turn through `MockTransport` and scans the JSONL. `rg '"args_str"|"dur_str"' src/` is empty.
3. **Commands.** `run_bash` with `cd /very/long/path && npm test` renders `$ npm test  in path/`, and the full command is in `args`.
4. **Failure.** A failed command shows the **last** 8 output lines, the exit code, and the expand hint.
5. **Memory reads.** A memory-read tool result renders as one human row; the rendered text contains no `{"`.
6. **Replay times.** The fixture dated 2026-01-01 renders `05:04` from its own timestamps, inside a `replayed from` separator.
7. **Missing mention.** `@nope.md hello` renders the warning chip under the user message, and no `✗` tool row.
8. **Detail levels.** `/details` switches to `normal`, the next tool call shows four preview lines, and earlier rows are not reprinted.
9. **Coverage.** BEAU-1's renderer coverage test still passes. Every registered event kind has a renderer and a snapshot in all three detail levels.

## 5. Rollout

- The session-log loader migrates old `args_str` and `dur_str` payloads once, on read.
- BEAU-2 and BEAU-3 are marked "Superseded in part by TUX-06 (visual blocks)".
- The `summarize` registry is public for plugin tools and documented in `docs/guides/` and `llms-full.txt`.

## 6. Verification

- `uv run pytest tests/unit/test_appender*.py tests/unit/test_tool_summary*.py tests/integration/test_session_log*.py tests/e2e/test_replay*.py -q`
- AGX-11's render benchmarks. The scrollback now prints less, so the frame budget improves.
- A before/after screenshot of the same replay.
