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
agenthicc session send SESSION_ID --text 'run the tests'
agenthicc session control SESSION_ID cancel
agenthicc session export SESSION_ID --output session.json
```

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
