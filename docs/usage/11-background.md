# Background sessions

Long-running work can be detached from the foreground TUI and managed
independently. The background supervisor owns worker leases, bounded process creation,
cancellation, and stale detection — it is a control plane, not a second agent
runtime.

## From the TUI

| Command or key | Effect |
|---|---|
| `/bg` (or `/background`, `/detach`) | Hand the current session to a background worker; the agents hub then opens with it selected |
| `/agents [running\|needs-you\|history\|library]` | Open the agents hub over the conversation; Esc returns to it |
| Ctrl+G, or ← on an empty prompt | Open the agents hub (← right after deleting asks for a second press) |
| `/inbox` | Open the hub's Needs you tab: pending approvals, questions and input requests |
| `/workers` | Open the hub's Running tab; `/workers --json` prints the worker manifests |

## From the CLI

`agenthicc agents` opens the agents hub full screen (`--tab`, `SESSION_ID`,
`--run RUN_ID`); its subcommands are the scriptable form.

```bash
agenthicc agents                     # the hub: running, needs you, history, library
agenthicc agents --tab needs-you     # open on one tab
agenthicc agents <id>                # open one agent's detail view
agenthicc agents list                # list background agents
agenthicc agents status <id>         # show one agent
agenthicc agents priority <id> urgent # set urgent, normal, or low priority
agenthicc agents cancel <id>         # stop an agent
agenthicc agents resume <id>         # resume an agent
agenthicc agents retry <id>          # retry a failed agent
agenthicc agents approve <id>        # approve a waiting agent
agenthicc agents reject <id>         # reject a waiting agent
agenthicc agents input <id>          # provide input to a waiting agent
agenthicc agents inbox list --json   # list pending typed interactions and versions
agenthicc agents inbox answer ITEM_ID TEXT VERSION KEY
agenthicc agents inbox approve ITEM_ID VERSION KEY
agenthicc agents inbox reject ITEM_ID VERSION KEY
agenthicc agents inbox expire ITEM_ID VERSION KEY
agenthicc agents rename <id>         # rename an agent
agenthicc agents labels <id>         # set comma-separated labels
agenthicc agents notifications       # inspect notification delivery receipts
agenthicc agents archive <id>        # archive an agent
agenthicc agents delete <id>         # move to recoverable trash
agenthicc agents restore <id>        # restore a deleted agent
agenthicc agents purge               # permanently remove expired trash
```

New requests use `normal` priority unless overridden. To request a specific
priority on admission, use `agenthicc run --background --priority low --intent
"..."` or `agenthicc --priority low --goal "..." --detach`. Durable queueing
is opt-in with `[background].queue_enabled = true`; otherwise a full worker
pool continues to return the existing capacity error. Queue positions shown in
JSON and the agents hub are advisory.

Useful flags: `agents list --all --json --trash` and `agents status --json`.

For one inbox covering approvals, questions, and explicit input prompts, use
the hub's Needs you tab (`/inbox`) or `agenthicc agents inbox list`. Each write is tied to the
listed item ID and version; use a fresh idempotency key for each intended
response. Multi-question `ask_user` answers are JSON objects keyed by question
ID. This inbox does not intercept or transform ordinary messages sent to an
agent from the hub's composer.

```bash
agenthicc agents list --json
```

```json
[
  {
    "approval_decision": null,
    "approval_request": "Workflow Design Review",
    "artifact_dir": "/root/.agenthicc/sessions/e8bc3147-02e5-4648-a16d-a8ad43e8e708",
    "attempt": 1,
    "cancellation_reason": "foreground handoff requested",
    "current_phase": "",
    "cwd": "/root/python_projects/python-password-generator",
    "error": null
  }
]
```

`artifact_dir` names the durable location to inspect, `attempt` distinguishes a
retry from a first run, and `cancellation_reason` records *why* a job stopped.

## When a job is parked

| Situation | Command |
|---|---|
| Waiting on an approval | `y`/`n` on it in the hub, or `agents status <id> --json`, then `agents approve` or `agents reject` |
| Waiting on a question | `i` on it in the hub, or `agents inbox answer` |
| Cancelled and missing from the list | `agents list --trash`, then `agents restore <id>` |

## What is durable

The store holds only the **rebuildable lifecycle index**. Kernel events,
conversation events, workflow phase state, approvals, and memory stay under
their existing owners, so a lost index is recoverable — and `purge`/`delete`
do not erase the underlying history.

Background notifications are opt-in and disabled by default. Configure
desktop notices or HTTPS webhooks in the `[background.notifications]` section;
see the [background-session guide](../guides/background-sessions.md#background-notifications-and-webhooks)
for the signed payload format, secret setup, private-network policy, and
delivery/recovery behavior. Inspect delivery receipts with:

```bash
agenthicc agents notifications --status failed --json
```

Receipt output contains event/session IDs, destination names, attempt counts,
result categories, and retry times. It deliberately omits payloads, webhook
URLs, and secret references.

## Next

- [Sessions →](07-sessions.md)
- [Troubleshooting →](12-troubleshooting.md)
- Depth: [Background sessions](../guides/background-sessions.md)
