# Your first task

This guide walks you through using agenthicc end-to-end. Every flag is
verified against `src/agenthicc/cli/parser.py`.

## Launch the interactive TUI

```bash
agenthicc
```

With no flags, agenthicc launches the Rich-Live TUI. Type a task in the
composer and press Enter.

## Headless / CI mode

```bash
agenthicc --headless            # read stdin lines, emit JSON-lines
```

In headless mode, each non-empty stdin line is an agent turn; results are
emitted as newline-delimited JSON on stdout. Ideal for pipelines and CI.

## Workflows

```bash
agenthicc --workflow code_plan          # run the built-in code-plan workflow
agenthicc --workflow code_plan --headless
```

`--workflow NAME` starts the TUI with NAME selected, or runs NAME per stdin
line in headless mode. Built-in workflows include `code_plan`, `site_imitate`,
and user-authored workflows.

## Mode selection

```bash
agenthicc --mode Safe    # read-only sandbox (default)
agenthicc --mode Plan    # read-only planning
agenthicc --mode Yolo    # full capabilities, no approval prompts
```

(Verified: `--mode` accepts Safe, Plan, or Yolo — `SELECTABLE_MODE_NAMES` in
`src/agenthicc/tui/runtime/mode_manager.py`.)

## Approvals

By default, tools that write, run commands, or touch the network require
approval. To disable approval prompts for a session:

```bash
agenthicc --dangerously-skip-permissions
```

Plan mode hard-blocks side effects even with this flag. This is intentionally
not settable in `agenthicc.toml`.

## Recording LLM calls (cassettes)

```bash
agenthicc --record-cassette            # record to ~/.agenthicc/cassettes
agenthicc --record-cassette ./out      # record to ./out/<session-id>/
```

Cassettes record LLM calls and approvals for replay and testing.

## Continue / resume

```bash
agenthicc --continue                   # most recent session in this dir
agenthicc --resume <session-id>        # a specific session
```

## Expected output

The TUI renders agent output, tool calls, and approvals in the scroll buffer
with a live status bar. Headless mode emits JSON-lines; each line is one
structured event.

## Next

- [The TUI →](04-tui.md)
- [Modes →](05-modes.md)
- [Sessions →](07-sessions.md)
