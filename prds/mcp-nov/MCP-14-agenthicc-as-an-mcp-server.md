---
title: "MCP-14: agenthicc as an MCP server — start and watch agenthicc work from another host"
status: Proposed (needs a product decision)
version: 1.0.0
date: 2026-11-28
priority: P3
related_prds:
  - MCP-00
  - MCP-05
  - MCP-13
  - COMM-05
  - prd-141
  - prd-172
  - SUBAGENT-19
---

# MCP-14 — agenthicc as an MCP server — start and watch agenthicc work from another host

This is a proposal that **reverses a recorded non-goal** and needs the maintainer's
decision before any work starts. PRD-172 §4.2 excluded "exposing agenthicc as an MCP
server. That is a separate product surface." The rest of this series makes agenthicc a
better MCP *client*. This PRD asks whether it should also be a server, and if so, how
small.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | The exclusion is recorded and was deliberate. | `prds/prd-172-…md` §4.2 |
| 2 | agenthicc has no API or server module. Its programmatic surface is the CLI: `run`, `agents list/status/cancel/resume/retry/rename/archive`, `runs list/show/resume`, `schedules …`, `session list`, `attach`. | `src/agenthicc/` (no `api` or `server` package); `cli/manifest.py:56-259,342`; `CLAUDE.md` ("The repository does not currently contain `agenthicc.api`") |
| 3 | The CLI sits on two reusable boundaries: `SessionService`, described as client-neutral, and the background supervisor and store that own session lifecycle, ownership and inputs. | `session_service.py`; `background/supervisor.py`, `background/store.py` |
| 4 | COMM-05 already built the rules for one session writing into another: a fixed untrusted header naming the sender, live sessions only, never reviving a finished one, a per-target limit. | `background/session_messages.py` |
| 5 | The server side of MCP is available: `@mcp_server`, `@mcp_tool`, `McpToolContext` (progress, elicitation), a stdio runner (`run_stdio_server`), Streamable HTTP with DNS-rebinding protection (`TransportSecuritySettings`), and `lmcp install` to register a server with Claude Desktop or Cursor. | `lauren-mcp` `README.md`; `_server/_stdio.py:26`; `_server/_transport_security.py` |
| 6 | The library's server side is also legacy-era today (MCP-13 row 4); upstream's dual-era server work is a P1 draft. | `lauren-mcp` `prds/2026-11-landscape/README.md` §2.1 |
| 7 | Other hosts in an operator's toolbox can use MCP servers but cannot drive agenthicc, so work agenthicc is good at (durable goal runs, workflows, background sessions) cannot be delegated to it. | MCP-00 |

## Goals (if approved)

1. A host such as Claude Code or Cursor can **list**, **start**, **watch**, **message** and
   **cancel** agenthicc background sessions and goal runs through MCP tools.
2. The authority that grants never exceeds what the operator already gives a local CLI
   user, and the caller cannot raise it.
3. The default is read-only and local: stdio, no listener, no write tools.

## Non-goals

- Exposing agenthicc's *tools* (filesystem, shell, git) as MCP tools. That would bypass the
  approval and workspace model; sessions are the unit of delegation, and each runs under
  its own mode.
- Remote access. HTTP exposure needs authorisation (an OAuth resource server) and a
  hosting story; it is a separate decision after this one.
- Multi-user operation. One server serves one local user's store.

## Design

### D1 — Launch

`agenthicc mcp serve [--workspace DIR] [--max-mode safe|plan|yolo] [--allow ACTIONS]`
speaks MCP over stdio. `--workspace` fixes the project root at launch, and every session it
starts uses it; a caller cannot choose another. `--max-mode` caps the mode of sessions it
starts (default `safe`). `--allow` is a comma list from `start,message,cancel,resume`;
absent, the server is read-only. `mcp serve` is registered with a host by a copy-pasteable
snippet that `agenthicc mcp serve --print-config` prints (the same shape as `lmcp install`
writes); it does not edit another host's files.

### D2 — Tools

| Tool | Class | What it does |
|---|---|---|
| `list_sessions` | read | background sessions of the workspace: id, title, status, workflow, last activity |
| `get_session(id)` | read | one session's status, mode, pending approval (what and why), result summary |
| `get_session_events(id, after, limit)` | read | bounded, redacted event page |
| `list_workflows` | read | the workflows this project offers, with parameters |
| `list_runs`, `get_run(id)` | read | durable goal runs and their phase |
| `start_session(intent, workflow="", mode="")` | write (`start`) | starts a background session; `mode` is clamped to `--max-mode` |
| `send_input(id, text)` | write (`message`) | writes to a **live** session; never revives a finished one |
| `cancel_session(id)`, `resume_session(id)` | write (`cancel`, `resume`) | lifecycle |

A resource template `agenthicc://sessions/{id}/transcript` returns a bounded, redacted
transcript; there are no prompts in v1. Tools are annotated honestly (`readOnlyHint` for the
read set, `destructiveHint` for `cancel_session`), knowing hosts treat them as advisory.

### D3 — Approvals stay with a human

A session started this way runs under the normal mode policy. An action that needs approval
leaves the session in `WAITING_APPROVAL`, visible in `agenthicc agents` and the hub and in
`get_session`. The calling host **cannot approve it**: there is no approval tool. An opt-in
`--forward-approvals` sends the request to the host through MCP elicitation so that host's
user sees it; it is off by default because an agent host could answer it without a person.

### D4 — Untrusted by construction

`send_input` text and the `intent` of `start_session` come from another agent. They are
recorded with their origin (`origin: mcp:<client name>`), framed for the receiving session
with the COMM-05 header adapted (`From MCP client "<name>"`, untrusted, cannot change
instructions, mode or permissions), and `send_input` uses the strict live path COMM-05
built. Per-client limits: at most 4 concurrent started sessions and 8 inputs per target per
hour (the COMM-05 constants).

### D5 — Redaction and bounds

Everything returned passes the session-export redactor already used for background
transcripts; the environment is never reflected; events and transcripts are paged and
capped; errors carry a rule name and no paths outside the workspace.

### D6 — Protocol

Built on the library's server (legacy era now; dual-era when U-server lands, MCP-13). Tool
results carry `structuredContent` with a schema, so hosts can parse them.

## Failure handling

| Case | Result |
|---|---|
| A write tool not in `--allow` | not listed; calling it by name returns `method_not_found` |
| `mode` above `--max-mode` | error `mode_exceeds_cap`; nothing is created |
| Unknown or finished session for `send_input` | `not_live`, no revival |
| A second `serve` for the same workspace | allowed (read-only paths are safe); lifecycle writes go through the existing store locks |
| Host disconnects | sessions it started keep running; they are ordinary background sessions |

## Security

This is the most delegation of authority in the series, so the design is conservative:
stdio and read-only by default; the project root and the mode ceiling are launch arguments
no caller can change; approvals are human; all caller text is untrusted and labelled; limits
are per client; nothing returns secrets. A prompt-injected host agent can start a Safe
session in the workspace, which is the same thing the operator's own terminal user can do and
is bounded by Safe-mode approvals. Remote exposure is explicitly out of scope.

## Rollout and migration

1. **Decision first** (below). If declined, mark this PRD *Not adopted* and record that
   PRD-172's exclusion stands.
2. D1–D2 read-only, then `start`, then `message`/`cancel`/`resume`, then D3's opt-in.
3. `docs/guides/mcp.md` gets a "Using agenthicc from another MCP host" section;
   `llms-full.txt`, `CHANGELOG.md`; PRD-172 §4.2 gets a pointer here.

## Decisions required

1. **Scope.** Does agenthicc become an MCP server at all? (Reverses PRD-172 §4.2.)
2. **Surface.** Is the tool set in D2 right, or should v1 be read-only?
3. **Approval forwarding.** Never, or opt-in as D3?

## Acceptance criteria

(If approved.)

1. Over stdio, `initialize` and `tools/list` succeed with only the read tools when `--allow` is
   absent; `--allow start` adds `start_session`.
2. `start_session(mode="yolo")` with `--max-mode safe` returns `mode_exceeds_cap` and
   creates nothing.
3. A started session that needs approval reaches `WAITING_APPROVAL`; no tool can approve it;
   `get_session` names the pending action.
4. `send_input` to a finished session returns `not_live`; to a live one it arrives framed as
   from the MCP client and recorded with its origin.
5. A transcript fetched through the resource contains no value from a planted environment
   variable that looks like a secret.
6. The server opens no listening socket in the default configuration.
7. agenthicc's own MCP client (MCP-03 harness) and `lmcp inspect` both list the tools and
   call `list_sessions`.

## Verification

Run the server as a subprocess under the MCP-03 client harness; socket check via the
process's open file descriptors; planted-secret test against the redactor; mutation: skip the
mode clamp, allow revival, reflect the environment.

## Risks

- A new, remotely reachable-in-effect control surface (through another host) needs ongoing
  security review; hence the conservative defaults and the explicit decision.
- Maintaining a second protocol role adds surface area to track upstream changes (MCP-13).
