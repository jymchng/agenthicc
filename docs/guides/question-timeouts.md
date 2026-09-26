# Interactive question timeouts

`ask_user` has a bounded wait so an unattended session cannot suspend an agent
turn forever. The default is 300 seconds (five minutes):

```toml
[tools]
question_timeout_s = 300.0
```

The setting can be overridden for one invocation:

```bash
agenthicc --set tools.question_timeout_s=180
```

Values must be finite and greater than zero. An unbounded value is deliberately
not supported. This timeout is independent of provider, agent-turn, HTTP,
browser, and plugin execution timeouts.

## What happens at the deadline

The shared `ApprovalService` owns the request deadline. The TUI displays a
countdown in the Questions overlay, refreshes it in place, and does not append
one transcript line per second. When the deadline expires, the overlay closes,
the pending request is cleared, and `ask_user` returns a structured result:

```json
{
  "timed_out": true,
  "decision_required": true,
  "request_id": "stable-request-id",
  "timeout_s": 300.0,
  "message": "The user did not answer before the configured deadline; choose the safest reasonable option, state the assumption, and do not ask the same question again solely because it timed out."
}
```

The runtime never invents an answer or treats a timeout as permission. The
agent receives the original conversation context and must make the safest
best-effort decision available, explicitly stating the assumption. If no safe
decision is possible, it uses the workflow's normal rejection or failure path.
A timeout is not a workflow phase transition.

## Cancellation, races, and resume

Esc, Ctrl-C, `/stop`, and session shutdown remain cancellation outcomes and do
not trigger the fallback decision. A response committed before the deadline
wins; a late response is rejected by request identity and cannot answer a later
question. Repeating the same question after it timed out in the same turn
returns a bounded repeated-timeout result instead of opening another wait.

Question lifecycle events (`question_wait_started`, `question_answered`,
`question_timed_out`, `question_cancelled`, and `question_wait_failed`) contain
only bounded identifiers, counts, deadlines, and outcomes. They do not contain
question text, answers, tool arguments, secrets, or provider output. On resume,
an unfinished wait is reconciled from its persisted deadline before the next
question dispatch; completed workflow phase/checkpoint state remains the source
of truth and is not reset to `INIT`.

Background sessions use the same deadline and reject input after expiry.
Headless mode returns its existing non-interactive cancellation result instead
of waiting for a UI that does not exist. Generated workflows inherit this
contract and must handle `timed_out=true` with an explicit assumption; they must
not implement a second question timer or bypass ordinary capability, workspace,
network, browser, MCP, or plugin policy.
