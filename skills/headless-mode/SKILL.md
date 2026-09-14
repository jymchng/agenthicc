---
name: headless-mode
version: 1.0.0
tags: [headless, json-lines, cli, automation, ci]
aliases: [headless-api]
description: >-
  Run agenthicc non-interactively with the --headless flag: one JSON-lines result
  per non-empty stdin line, the stdout event contract, workflow execution through
  run_headless_workflow, and exit behavior for pipelines and CI.
---

# Skill: Headless Mode

Headless mode runs a workflow with no terminal UI. It reads intents from stdin
and writes one JSON object per line to stdout, so a shell script or CI job can
drive agenthicc without a pseudo-terminal and parse the result with `jq`.

There is no HTTP server, no REST API, and no WebSocket transport in agenthicc.
`agenthicc --headless` is a **stdin/stdout JSON-lines process**, and that is the
whole integration surface.

## When to use this skill

Use this skill when you need to:

- Run a workflow from a script, CI job, or another process without a TUI
- Feed several intents to one long-lived session and read one result each
- Understand the exact stdout record schema so you can parse it
- Decide how approvals behave when no operator is present
- Call the headless runner programmatically from Python

Do **not** use this skill for interactive work — see `running-the-tui` — or for
cassette-based regression testing — see `testing-agenthicc`.

---

## The two entry points

| Entry point | Module | Use when |
|---|---|---|
| `agenthicc --headless --workflow NAME` | `src/agenthicc/cli/parser.py:18` | A process should keep reading stdin lines |
| `agenthicc workflows run NAME --intent TEXT` | `src/agenthicc/cli/commands/workflows.py:98` | You want exactly one intent and an exit |

Both ultimately call the same async runner, `run_headless_workflow`
(`src/agenthicc/runners/headless.py:494`), which builds a durable session, runs
one workflow, and closes its resources.

### `--headless` works with or without `--workflow`

`--headless` and `--workflow` are distinct global flags
(`src/agenthicc/cli/parser.py:16-30`):

```text
--headless          Run without the TUI; emit JSON-lines to stdout.
--workflow NAME     Start the TUI with NAME selected, or run NAME for each
                    stdin line in headless mode.
```

`_run_headless` branches on `ctx.workflow_name`
(`src/agenthicc/runners/headless.py:700-703`), which gives two different
headless behaviours:

- **With `--workflow NAME`** it delegates to `_run_headless_workflow_stream`, so
  every input line runs the whole workflow and yields a `WorkflowRunCompleted`
  object.
- **Without `--workflow`** it falls through to a plain session loop that submits
  each line as a message and emits an `IntentCreated` object instead. Its
  `ready` banner has no `workflow` key.

The streaming function re-checks the name defensively
(`raise ValueError("--workflow requires a workflow name")`,
`src/agenthicc/runners/headless.py:566-567`), but the guard above makes that
branch unreachable from the CLI. The error you will actually hit is an unknown
name: `--headless --workflow bogus` raises
`ValueError("Unknown workflow 'bogus'. Available: ...")`
(`src/agenthicc/runners/tui_session.py:968`).

Use `--workflow` alone, without `--headless`, to open the TUI with a workflow
already selected.

---

## The stdout contract

The streaming loop is `_run_headless_workflow_stream`
(`src/agenthicc/runners/headless.py:561`), reached when you pass
`--headless --workflow NAME`. It emits exactly two kinds of line, both flushed
immediately.

### 1. One `ready` banner, before any input is read

```json
{"status": "ready", "mode": "headless", "workflow": "code_plan", "session_id": "67bab0e3-fb9d-4bbc-b971-70d5e9785b49"}
```

Read this first if your script needs the session id (for example, to reconstruct
the session later with `--resume`).

### 2. One result object per non-empty input line

Each non-empty stdin line is treated as a complete intent. Blank or
whitespace-only lines are skipped, and EOF ends the loop cleanly
(`src/agenthicc/runners/headless.py:620-624`).

The result is `WorkflowExecutionResult.to_dict()`
(`src/agenthicc/runners/headless.py:54-76`):

```json
{
  "event_type": "WorkflowRunCompleted",
  "session_id": "67bab0e3-fb9d-4bbc-b971-70d5e9785b49",
  "workflow": "code_plan",
  "run_id": "b1c2d3e4",
  "status": "complete",
  "phases": ["plan", "execute", "review", "summarize"],
  "error": null
}
```

`phase_metadata` is added only when the run recorded any.

| Field | Meaning |
|---|---|
| `event_type` | Always `"WorkflowRunCompleted"` |
| `session_id` | Durable session id, also in the `ready` banner |
| `workflow` | The workflow name you passed |
| `run_id` | Run id for this turn; `""` when the turn failed before starting |
| `status` | Workflow outcome, e.g. `complete` (see below) |
| `phases` | Phase names that completed, in order |
| `error` | `null` on success, else a `"TypeName: message"` string |

A failure is reported **in-band**, not by a non-zero exit: the loop catches the
exception and emits a result with `status: "failed"`, an empty `run_id`, and the
exception text in `error` (`src/agenthicc/runners/headless.py:638-646`). Always
check `status` rather than relying on the process exit code.

### Variant: `--headless` without `--workflow`

The no-workflow branch prints a `ready` banner **without** a `workflow` key
(`src/agenthicc/runners/headless.py:785-788`):

```json
{"status": "ready", "mode": "headless", "session_id": "6801c8d62aba454c97845ff186464887"}
```

and then one `IntentCreated` line per non-empty input line
(`src/agenthicc/runners/headless.py:822-831`):

```json
{"event_type": "IntentCreated", "intent_id": "0ef3ca7cbba4493ba1b31ae6651ed74b", "status": "pending"}
```

So the per-line record is **not** always `WorkflowRunCompleted`: without
`--workflow` there is no workflow run to report, and your `jq` selectors must
match on `intent_id`/`status` instead. If a script needs a final workflow
verdict, pass `--workflow NAME`.

---

## Recipe: drive one session from a shell pipeline

```bash
printf '%s\n' \
  "add a glossary page to the docs" \
  "fix the broken anchor in README.md" \
  | agenthicc --headless --workflow code_plan \
  > results.jsonl

# The first line is the ready banner; the rest are results.
tail -n +2 results.jsonl | jq -r 'select(.status != "complete") | .error'
```

Feed the intents one per line. Because the loop reuses a single session, the
second intent runs in the same durable session as the first — useful when the
second request depends on files the first one wrote.

## Recipe: one intent, asked as a question

```bash
agenthicc workflows run code_plan \
  --intent "add a glossary page to the docs" \
  --json
```

`workflows run` maps its parameters straight onto argparse: `workflow_name`
has no default so it is positional, `intent` defaults to `""` so it becomes
`--intent TEXT`, and `json` is a boolean so it becomes the `--json` flag
(`src/agenthicc/cli/commands/workflows.py:98-101`;
signature-inference rules in `src/agenthicc/cli/registry.py:81-85`).

---

## Approvals in headless mode

There is no operator, so `_HeadlessApprovalService` **defaults to denying**
every approval-gated action (`src/agenthicc/runners/headless.py:70-80`). The
docstring is explicit that this avoids "hanging forever waiting for a UI
response".

That default is intentional: a headless run is fail-closed. To let a trusted
automation run unattended, opt in explicitly:

```bash
agenthicc --headless --workflow code_plan --dangerously-skip-permissions
```

`--dangerously-skip-permissions` (`src/agenthicc/cli/parser.py:89`) sets
`CLIFlags.dangerously_skip_permissions`
(`src/agenthicc/cli/context.py:20`), which the headless runner passes as the
`allow` argument to `_HeadlessApprovalService`. Only use it in a sandbox you
control.

---

## Calling the runner from Python

```python
import asyncio

from agenthicc.cli.context import CLIContext
from agenthicc.runners.headless import (
    WorkflowExecutionResult,
    run_headless_workflow,
)

async def main() -> None:
    ctx = CLIContext(workflow_name="code_plan", headless=True)
    result: WorkflowExecutionResult = await run_headless_workflow(
        ctx,
        "code_plan",
        "add a glossary page to the docs",
    )
    if result.status != "complete":
        raise SystemExit(f"{result.workflow} failed: {result.error}")
    print(result.to_dict())

asyncio.run(main())
```

`agenthicc.runners.headless.__all__` is exactly
`["WorkflowExecutionResult", "execute_workflow", "run_headless_workflow"]`
(`src/agenthicc/runners/headless.py:25`).

---

## Related flags that matter in automation

| Flag | Effect |
|---|---|
| `--config PATH` | Use a specific `agenthicc.toml` instead of discovery |
| `--mode MODE` | Start in `Safe`, `Plan`, or `Yolo` (aliases accepted) |
| `--set key=value` | Override a config value for this run |
| `--set-secret key=value` | Override a secret config value for this run |
| `--resume ID` / `--continue` | Continue a previous session |
| `--record-cassette DIR` | Record the run as a replayable cassette |

`--resume` and `--continue` matter for pipelines: the headless runner resolves
an owner lease before building the session
(`src/agenthicc/runners/headless.py:503`), so two processes cannot drive the
same session at once.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: Unknown workflow 'X'` | `--workflow X` names no available workflow | Pick a name from the `Available:` list in the error |
| First output line is not a result | It is the `ready` banner | Skip line 1, e.g. `tail -n +2` |
| Result lines are `IntentCreated`, not `WorkflowRunCompleted` | `--headless` was used without `--workflow` | Add `--workflow NAME` |
| Blank stdin lines produce no output | Empty lines are skipped by design | Send one non-empty intent per line |
| Run reports `status: "failed"` but exit code is 0 | Failures are in-band | Assert on `.status`, not `$?` |
| Approval-gated tool is denied | Headless approvals fail closed | Pass `--dangerously-skip-permissions` only if trusted |
| `SessionAlreadyActiveError` | Another process holds the session lease | Wait, or use `--resume` on a finished session |

---

## Key points

- Headless mode is **stdin JSON-lines**, not HTTP/WebSocket. No server exists.
- `--headless` runs a no-workflow session loop on its own; add `--workflow NAME`
  to get one `WorkflowRunCompleted` object per non-empty intent line.
- Output is one `ready` banner, then one result object per non-empty intent line.
- Check `status` in the result; a failed run still exits 0.
- Approvals default to deny, so headless runs are safe by default and require
  an explicit opt-in to skip.
- The programmatic entry point is `run_headless_workflow(ctx, name, intent)`.
