# Client-neutral sessions

PRD-150 adds one session contract for the TUI, headless runner, CLI, and
future web/IDE clients. The canonical implementation is
`agenthicc.session_service.SessionService`; it is intentionally not under the
historical `agenthicc.api` path.

## Session lifecycle

Every session has a stable ID, a policy-filtered `SessionSnapshot`, and a
per-session durable event sequence. Clients submit `SessionCommand` values (or
JSON objects with the same fields) and consume `SessionEvent` values. A
reconnecting client stores its last durable sequence, requests a fresh
snapshot, and replays events after that cursor.

Commands with an `idempotency_key` are recorded in the service ledger. A
retrying client receives the original `CommandResult` with `replayed: true`
and does not queue a second turn or repeat a control side effect. An
`expected_sequence` makes concurrent conflicting writes fail closed with a
`stale_sequence` error.

## CLI

```bash
agenthicc session create --project-root . --workflow code_plan
agenthicc session list --json
agenthicc session show SESSION_ID --json
agenthicc session events SESSION_ID --after 0
agenthicc session send SESSION_ID 'run the tests'
agenthicc session control SESSION_ID cancel
agenthicc session control SESSION_ID fork --payload '{"turn": 3}'
agenthicc session export SESSION_ID --output session.json
```

`send` and `control` take their arguments **positionally**:
`session send SESSION_ID TEXT` and `session control SESSION_ID KIND`. `control`
additionally accepts `--payload` holding a JSON object; `send` has no
`--text`/`--message` flag and passing one fails with
`unrecognized arguments`. Run `agenthicc session send --help` before quoting a
signature from memory.

The existing plural `sessions` commands remain compatibility tools for the
kernel/conversation artifacts. `sessions list` opens a paginated interactive
selector in a terminal; pressing Enter resumes the selected session and loads
its transcript. When output is redirected, it prints a deterministic page
instead. It merges the historical project-local index with the current
user-wide TUI index, so it shows sessions created by both runtime generations.
The singular `session` group reads the client-neutral projection and is the
path new adapters should use.

## Local attachment

`agenthicc session serve` starts an explicit local HTTP/SSE adapter. It binds
to `127.0.0.1` by default and exposes health, snapshot, durable replay, live
stream, and command endpoints. A non-loopback bind is rejected unless a
bearer token is configured. CORS origins are not opened by default, and the
transport never constructs an agent runner.

Web and IDE integrations should use `HttpSessionClient` (or the named
`WebSessionAdapter`/`IdeSessionAdapter`) and depend only on the public models.
They must not import Rich widgets, reactive TUI state, kernel reducer state, or
session journal files.

## Security and retention

Capability checks happen before snapshots, replay, and commands. The service
redacts credential-shaped keys and hides project roots unless the client has
workspace capability. Event subscriptions have bounded queues; overflow
returns a typed `backpressure` error so the client can reconnect from its
cursor. Compaction is explicit and produces a typed replay gap rather than
silently returning an incomplete history.

## A runnable round trip

The client-neutral CLI path is read-only until you actually submit a message,
so it is safe to explore against an existing project root. `session list
--json` prints the policy-filtered snapshots as an array:

```bash
PYTHONPATH=src python -m agenthicc session list --json
```

```text
[
  {
    "schema_version": 1,
    "session_id": "84348746-7ff0-43a5-b2c3-0cd9e04a9ff8",
    "project_root": "/root/python_projects/agenthicc",
    "created_at": 1789366301.8633466,
    "updated_at": 1789369125.3590968,
    "state": "running",
    "active_turn_id": null,
    "parent_session_id": null,
    "workflow": {},
    "agent": {},
    "queue": {
      "depth": 0,
      "accepting_input": true
    },
    "approvals_pending": 0,
    "questions_pending": 0,
    "background_jobs_running": 0,
    "terminals_running": 0,
    ...
  }
]
```

`session_id`, timestamps, and `project_root` obviously differ per machine. The
*shape* is the contract: `schema_version` is present so adapters can refuse a
projection they do not understand, `project_root` is populated only because the
CLI client holds the `workspace` capability, and the queue/turn fields are
numeric or null rather than free text.

To replay one session's durable events from a cursor, pass the sequence number
you last processed:

```bash
PYTHONPATH=src python -m agenthicc session events SESSION_ID --after 0
```

```text
{"durability": "durable", "event_id": "evt_80ab4b1f...", "kind": "session_created", "occurred_at": 1788439951.3006477, "payload": {"capabilities": ["control", "read", "workspace"], "project_root": "/root/python_projects/agenthicc"}, "schema_version": 1, "sequence": 1, "session_id": "...", "source": "session_service", "turn_id": null, "visibility": "session"}
{"durability": "durable", "event_id": "evt_6f038317...", "kind": "user_message", "occurred_at": 1788440030.0578258, "payload": {"text": "..."}, "schema_version": 1, "sequence": 2, "session_id": "...", "source": "tui", "turn_id": null, "visibility": "session"}
```

Fields are emitted with sorted keys and every event carries the same envelope:
`sequence` (the cursor you resume from), `kind`, `payload`, `source`,
`durability`, and `visibility`. Each event is one JSON object on its own line,
which is what makes `--after CURSOR` resumable after a reconnect.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `unrecognized arguments: --text` | `session send` takes `TEXT` positionally; there is no `--text`/`--message` flag | Use `agenthicc session send SESSION_ID 'the message'` |
| `unrecognized arguments: --payload` when sending a message | `--payload` belongs to `session control`, not `session send` | Use `agenthicc session control SESSION_ID KIND --payload '{"k": "v"}'` |
| Command fails with `stale_sequence` | Another client wrote to the session since your cursor | Re-read the snapshot with `session show`, then retry with the new `expected_sequence` |
| Same message queued twice | You resent without an `idempotency_key` | Supply an `idempotency_key`; a retry then returns the original result with `replayed: true` |
| `backpressure` error on a subscription | The bounded per-subscriber queue overflowed | Reconnect from your stored cursor instead of resubscribing from zero |
| `project_root` is null in a snapshot | The caller lacks the `workspace` capability | Grant workspace capability, or read the redacted view and stop expecting the path |
| Credential-shaped values appear as `[redacted]` | Intentional redaction in the projection | Do not log around it; use the service, which never returns the raw value |
| `session serve` refuses to bind | A non-loopback bind without a bearer token | Configure `--auth-token`, or keep the default `127.0.0.1` bind |

### `sessions` and `session` are different groups

The plural `sessions` group reads kernel/conversation artifacts and paginates
with `--page`/`--page-size`; the singular `session` group reads the
client-neutral projection and is the path new adapters should use. Passing
`--page` to `session list` fails, and `--project-root` does not exist on
`sessions list`.

### Replay returned a gap instead of history

Compaction is explicit and produces a typed replay gap rather than silently
returning an incomplete history. Treat a gap as authoritative: re-snapshot
before continuing rather than assuming you missed only cosmetic events.

### An adapter imports a Rich widget or the reducer

Adapters must depend only on the public models. Importing Rich widgets,
reactive TUI state, kernel reducer state, or session journal files couples the
adapter to internals that are free to change. Use `HttpSessionClient` (or the
named `WebSessionAdapter`/`IdeSessionAdapter`).
