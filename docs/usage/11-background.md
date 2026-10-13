# Background sessions

Long-running work can be detached from the foreground TUI and managed
independently. The manager owns worker leases, bounded process creation,
cancellation, and stale detection — it is a control plane, not a second agent
runtime.

## From the TUI

| Command | Effect |
|---|---|
| `/bg` (or `/background`) | Move the current task into the background |
| `/bg list` | List running background sessions |
| `/bg <n>` | Re-attach and replay buffered output |
| `/inbox` | Review pending background approvals, questions, and input requests |

## From the CLI

`jobs` is the scriptable form; `agents` opens the same manager UI.

```bash
agenthicc jobs list                # list background sessions
agenthicc jobs status <id>         # show one session
agenthicc jobs priority <id> urgent # set urgent, normal, or low priority
agenthicc jobs cancel <id>         # cancel a session
agenthicc jobs resume <id>         # resume a session
agenthicc jobs retry <id>          # retry a failed session
agenthicc jobs approve <id>        # approve a waiting session
agenthicc jobs reject <id>         # reject a waiting session
agenthicc jobs input <id>          # provide input to a waiting session
agenthicc jobs inbox list --json   # list pending typed interactions and versions
agenthicc jobs inbox answer ITEM_ID TEXT VERSION KEY
agenthicc jobs inbox approve ITEM_ID VERSION KEY
agenthicc jobs inbox reject ITEM_ID VERSION KEY
agenthicc jobs inbox expire ITEM_ID VERSION KEY
agenthicc jobs rename <id>         # rename a session
agenthicc jobs labels <id>         # set comma-separated labels
agenthicc jobs notifications       # inspect notification delivery receipts
agenthicc jobs archive <id>        # archive a session
agenthicc jobs delete <id>         # move to recoverable trash
agenthicc jobs restore <id>        # restore a deleted session
agenthicc jobs purge               # permanently remove expired trash
```

New requests use `normal` priority unless overridden. To request a specific
priority on admission, use `agenthicc run --background --priority low --intent
"..."` or `agenthicc --priority low --goal "..." --detach`. Durable queueing
is opt-in with `[background].queue_enabled = true`; otherwise a full worker
pool continues to return the existing capacity error. Queue positions shown in
JSON and the `agents` manager are advisory.

Useful flags: `jobs list --all --json --trash` and `jobs status --json`.

For one inbox covering approvals, questions, and explicit input prompts, use
`/inbox` in the TUI or `agenthicc jobs inbox list`. Each write is tied to the
listed item ID and version; use a fresh idempotency key for each intended
response. Multi-question `ask_user` answers are JSON objects keyed by question
ID. This inbox does not intercept or transform ordinary messages sent from a
session's details view.

```bash
agenthicc jobs list --json
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
| Waiting on an approval | `jobs status <id> --json`, then `jobs approve` or `jobs reject` |
| Waiting on a question | `jobs input <id>` |
| Cancelled and missing from the list | `jobs list --trash`, then `jobs restore <id>` |

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
agenthicc jobs notifications --status failed --json
```

Receipt output contains event/session IDs, destination names, attempt counts,
result categories, and retry times. It deliberately omits payloads, webhook
URLs, and secret references.

## Next

- [Sessions →](07-sessions.md)
- [Troubleshooting →](12-troubleshooting.md)
- Depth: [Background sessions](../guides/background-sessions.md)
