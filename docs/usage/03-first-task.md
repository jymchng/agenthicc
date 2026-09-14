# Your first task

An end-to-end walkthrough. Every flag is verified against
`src/agenthicc/cli/parser.py`.

## 1. Launch the interactive TUI

```bash
agenthicc
```

With no flags, agenthicc opens the Rich-Live TUI. Type a task in the composer
and press Enter.

## 2. Or run headless

```bash
agenthicc --headless
```

Each non-empty stdin line becomes an agent turn, and results are written as
newline-delimited JSON. This is the mode to use in pipelines and CI:

```bash
printf 'summarise the tool registry\n' | agenthicc --headless
```

With no input it still emits a readiness record and exits 0, which makes it a
safe smoke test:

```bash
agenthicc --headless < /dev/null
```

```text
{"status": "ready", "mode": "headless", "session_id": "4c783bb1ff244a66bf3243d28aead0da"}
```

`session_id` is a fresh value per run; the keys and `"status": "ready"` are
what stay stable. Because no workflow was selected, per-line records are
`IntentCreated`-class events, not a `WorkflowRunCompleted` summary.

## 3. Pick a mode

```bash
agenthicc --mode Safe    # prompt before side effects (default)
agenthicc --mode Plan    # plan only; side effects hard-blocked
agenthicc --mode Yolo    # no per-action prompts
```

Modes are the main safety dial; details and aliases are in [Modes](05-modes.md).

## 4. Run a workflow

```bash
agenthicc --workflow code_plan          # TUI with code_plan selected
agenthicc --workflow code_plan --headless
```

Eight workflows ship built in. Confirm the list rather than trusting a table:

```bash
agenthicc workflows list
```

```text
code_plan [builtin] — Plan → Execute → Review → Summary
  phases: plan → execute → review → summarize
  modes: Plan
copy_website [builtin]
create_workflow [builtin]
goal_flow [builtin]
make_agenthicc_tool [builtin]
make_book [builtin]
reconstruct_site [builtin]
site_imitate [builtin]
```

`code_plan` also answers to the alias `Plan`.

## 5. Approvals

By default, tools that write, run commands, or touch the network require
approval in Safe mode. To auto-approve capability prompts for one session:

```bash
agenthicc --dangerously-skip-permissions
```

Plan still hard-blocks side effects, and this flag cannot be stored in
`agenthicc.toml`. See [Security](10-security.md).

## 6. Record calls for replay

```bash
agenthicc --record-cassette            # write to ~/.agenthicc/cassettes
agenthicc --record-cassette ./out      # write to ./out/<session-id>/
```

Cassettes capture LLM calls and approvals so a run can be replayed in tests.

## 7. Come back later

Every run creates a session. Two flags reload prior context:

```bash
agenthicc --continue          # carry on with the latest one here
agenthicc --resume <id>       # a session you name explicitly
```

Use `--continue` for the common "carry on where I left off" case, and `--resume`
when you need one specific session. [Sessions](07-sessions.md) covers the
transcript rules.

## Next

- [The TUI →](04-tui.md)
- [Modes →](05-modes.md)
- [Sessions →](07-sessions.md)
- Depth: [Quickstart](../guides/quickstart.md) · [Workflows](../guides/workflows.md)
