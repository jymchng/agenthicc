---
title: "MCP-09: Servers can ask the operator, through one host-side input model"
status: Proposed
version: 1.1.0
date: 2026-11-28
priority: P2
related_prds:
  - MCP-00
  - MCP-02
  - MCP-03
  - MCP-05
  - MCP-13
  - prd-172
  - AGI-03
---

# MCP-09 — Servers can ask the operator, through one host-side input model

A server can ask its client for three things: to **ask the user** (elicitation), to **run
the client's model** (sampling) and to **name the client's directories** (roots). agenthicc
advertises none of them, so a server that needs any of them fails or degrades. PRD-172
deferred all three "until the host approval and user-question contracts are specified".
Those contracts now exist, and the specification has moved on: it **deprecates sampling and
roots** and replaces all three request kinds with a multi round-trip pattern. This PRD
therefore builds elicitation, builds it once so it survives the move, and **records a
decision not to adopt sampling or roots**.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | The client is built with no `roots`, `sampling_handler` or `elicitation_handler`; `initialize` advertises an empty capability set and the library answers every such request `METHOD_NOT_FOUND`. | `tools/mcp.py:332-362`; `lauren-mcp` `_client/_features.py:157-170,197-240` |
| 2 | PRD-172 §11 Phase 3 item 4 deferred them "until the host approval/user-question contracts are specified". | `prds/prd-172-…md:629-638` |
| 3 | The user-question contract exists and is bounded: `ApprovalRequest(kind="questions")` through `ApprovalBroker.request_input`, an overlay in the TUI and the operator inbox in a background session. Questions are `{id, text, options}` with `options` non-empty and an always-present free-text "Other"; the wait ends in `cancelled` or `timed_out` (five minutes by default). | `tools/approval.py:81-92,132`; `workflows/code_plan/phase_tools.py` (`make_questions_tool`, `_validate_questions`); `background/worker.py:902,1091` |
| 4 | Elicitation in the legacy revision (2025-06-18): flat object schemas of primitives; three response actions `accept`, `decline`, `cancel`; servers **MUST NOT** request sensitive information; clients **SHOULD** make clear which server asks, let users review, decline and cancel, validate content and rate-limit. | spec 2025-06-18, Client → Elicitation, read 2026-11-28 |
| 5 | In the current revision (2026-07-28) server-initiated `roots/list`, `sampling/createMessage` and `elicitation/create` are **replaced by Multi Round-Trip Requests**: the server returns an `InputRequiredResult` (`resultType: "input_required"`) with `inputRequests`, and the client retries the original request with `inputResponses`. | spec 2026-07-28, Changelog item 7 |
| 6 | In the same revision **Roots, Sampling and Logging are Deprecated**: "new implementations should not add support for them". Earliest removal: the first revision released on or after 2027-07-28. Migrations: pass directories via tool parameters, resource URIs or server configuration; integrate directly with LLM provider APIs. | spec 2026-07-28, Deprecated Features |
| 7 | `notifications/elicitation/complete` and the `elicitationId` of URL-mode elicitation were removed in 2026-07-28; the client learns the outcome by retrying the request. | spec 2026-07-28, Changelog minor item 11 |
| 8 | The `_looks_secret` test already used for configuration names secret-looking keys. | `tools/mcp_server_config.py:_looks_secret` |

## Goals

1. A server that elicits gets a real answer from the operator, and a server that is
   declined or cannot be answered gets a well-formed `decline` or `cancel`.
2. One host-side model, `McpInputRequest`, serves the legacy `elicitation/create` request
   and the modern `inputRequests` entry, so the move between eras (MCP-13) changes only the
   adapter.
3. The capability is advertised only when the host can honour it in the current session
   type.
4. A decision about sampling and roots is on record, with the conditions to reopen it.

## Non-goals

- URL-mode elicitation and any flow that opens a browser (authorization is MCP-10).
- Adopting sampling or roots (see "Not adopted").
- Answering a server's questions with the model. Elicitation goes to the operator.

## Design

### D1 — Setting

```toml
[[tools.mcp_servers]]
name = "db"
elicitation = "ask"     # "off" | "ask"   (default "ask")
```

The `elicitation` capability is declared in `initialize` (legacy) or the per-request
capabilities (modern) only when the setting is not `off` and the session can honour it:
the foreground TUI, or a background session with an operator inbox. Plain headless sessions
do not advertise it. A project-layer value is ignored until the server is trusted (MCP-05).

### D2 — The host-side model

```python
@dataclass(frozen=True, slots=True)
class McpInputRequest:
    server: str
    kind: Literal["elicitation"]          # the only kind this PRD supports
    message: str
    schema: Mapping[str, object]          # flat object of primitive properties
    state: str = ""                       # opaque requestState (modern era), echoed back untouched

@dataclass(frozen=True, slots=True)
class McpInputOutcome:
    action: Literal["accept", "decline", "cancel"]
    content: Mapping[str, object] = field(default_factory=dict)
```

The adapter produces requests from `elicitation/create` or from `inputRequests`, and turns
an outcome into the JSON-RPC result or the `inputResponses` entry.

### D3 — From schema to questions

One request becomes one `kind="questions"` request through the existing contract, headed
`MCP server "<server>" asks:` followed by the server's `message`. Each property becomes a
question:

| Property | Question |
|---|---|
| `enum` (with optional `enumNames`) | options are the names; the answer maps back to the value |
| `boolean` | options `true`, `false` |
| `string`, `number`, `integer` | the `default` (or an `(answer below)` placeholder) as the single option; the overlay's free-text "Other" takes the typed value |

The reply is validated against the property (type, `minimum`/`maximum`,
`minLength`/`maxLength`, formats `email`, `uri`, `date`, `date-time`); an invalid value
re-asks once, then returns `cancel`. An explicit **Decline** choice is added to the first
question and returns `decline`; dismissal and the existing timeout return `cancel`;
submission returns `accept` with typed `content`.

### D4 — Guards, in order

1. A property whose name or title looks like a secret (`_looks_secret`) is auto-declined
   with a notice: the specification forbids servers from asking for sensitive information.
2. A schema that is not a flat object of supported primitives is declined with `-32602`.
3. At most 5 requests per server per minute; beyond that they are declined and counted in
   `/mcp status`.
4. One overlay at a time per session; the rest are queued.

### D5 — Background sessions

Background: the request goes to the operator inbox exactly as `ask_user` and tool approvals
do (`BackgroundApprovalService.request_input`), with the same bounded wait. An unanswered
request ends `cancel`.

## Not adopted: sampling and roots

**Decision (proposed): do not implement `sampling/createMessage` or `roots/list`.** Reasons:
both are Deprecated in the current revision with "new implementations should not add
support"; sampling lets a server spend the operator's tokens and run text through the
operator's model, which is a security surface for a feature scheduled for removal; and the
specification's migrations (tool parameters, resource URIs, server configuration, direct
provider integration) are within reach of the servers that need them.

A server that requires roots is configured with its directories as arguments (the
`command` argv and `cwd` already allow it). `mcp doctor` reports when a connected server's
capabilities show it expects roots or sampling.

**Reopen if** a server the operator depends on cannot work without one of them. If sampling
is reopened, the constraints are: off by default; per-server opt-in that a project config
cannot set; every request shown to the operator before it runs and every response before it
is returned; a single `transport.complete` call with **no tools and no conversation**; a
per-server token budget and a rate limit; usage recorded in the usage ledger with the
server's name. Those are the limits the legacy specification asks for (a human in the
loop, review of request and response, rate limiting) and the compactor already shows the
call and metering path (`memory/compactor.py:1104-1135`).

## Failure handling

| Case | Result |
|---|---|
| No answer within the bounded wait | `cancel` |
| Server disconnects while an overlay is open | overlay closes; the late answer is dropped |
| Request for a capability not advertised | `METHOD_NOT_FOUND`, as now |
| Rate limit exceeded | `decline`, counted |
| A `roots` or `sampling` entry inside modern `inputRequests` | answered `decline` with a one-line reason |

## Security

Elicitation answers go to the server named in the overlay and never to the model. The
server's `message` is untrusted: plain text, length-bounded, control sequences removed. The
secret guard and the rate limit exist because the specification puts the obligation not to
ask for sensitive data on the server and the obligation to notice on the client. The
overlay always names the server.

## Rollout and migration

1. Needs MCP-02 (the adapter forwards the handler) and MCP-05 (a project config cannot turn
   the capability on by itself).
2. Ship D2–D5 with the legacy request. MCP-13 adds the modern `inputRequests` mapping to
   the same model.
3. Update `docs/guides/mcp.md`, `docs/guides/configuration.md`, `llms-full.txt`,
   `CHANGELOG.md`; mark PRD-172 Phase 3 item 4 delivered for elicitation and answered for
   the rest.

## Acceptance criteria

Against the MCP-03 `elicits` scenario.

1. The `initialize` frame's capabilities include `elicitation` only with an approval service
   and `elicitation != "off"`.
2. An enum + integer + boolean schema produces three typed answers in `content`; an
   out-of-range integer is re-asked once, then returns `cancel`.
3. Decline, dismissal and timeout return `decline`, `cancel` and `cancel`.
4. A property named `password` is declined with no overlay and a notice.
5. The sixth request in a minute is declined and counted.
6. In a background session the request appears as an operator-inbox item; unanswered it
   ends `cancel`.
7. A project-layer `elicitation` setting has no effect until the server is trusted.
8. `grep -rn "sampling_handler\|roots=" src/agenthicc` finds nothing: the decision above is
   enforced by the absence of the handlers.

## Verification

Peer frame logs for the wire; the real `ApprovalBroker` for the host side; mutation checks:
skip schema validation, skip the secret guard, advertise the capability with no approval
service.

## Risks

- Elicitation questions are shaped by `ask_user`'s multiple-choice contract and use its
  free-text "Other" for typed values. If that proves clumsy, a typed-field overlay is a
  follow-up that does not change this PRD's contract.
- A server that insists on sampling or roots will not work until the decision is reopened;
  `mcp doctor` names the reason.
