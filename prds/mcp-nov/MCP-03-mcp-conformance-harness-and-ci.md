---
title: "MCP-03: A conformance harness — the real client against real servers, in CI"
status: Proposed
version: 1.0.0
date: 2026-11-28
priority: P1
related_prds:
  - MCP-00
  - MCP-01
  - MCP-02
  - prd-31
  - prd-172
  - TOOLR-06
---

# MCP-03 — A conformance harness: the real client against real servers, in CI

Every MCP behaviour agenthicc promises is tested against a stand-in, not against
the client library it ships. This PRD adds a small peer server that agenthicc
controls, runs it over all four transports, and makes the real client part of CI,
so each later MCP PRD has a place to prove its acceptance criteria.

## Evidence

| # | What is true today | Where |
|---|---|---|
| 1 | CI and nox install only `--extra dev`; `lauren-mcp` is not in it, so no job imports the client. (MCP-01 row 2.) | `noxfile.py:30`, `pyproject.toml:30-47` |
| 2 | The bridge, manager and registry tests use fake clients and raw objects. The integration fixture builds `ToolSchema` objects "try the real lauren_mcp types first", else raw stand-ins. | `tests/integration/conftest.py:22-45`; `tests/unit/test_mcp_bridge.py` |
| 3 | The only test against a real server is skipped unless `AGENTHICC_MCP_E2E` is set and `npx` can fetch `@modelcontextprotocol/server-filesystem`. It asserts that at least one tool name contains `list` or `read`. | `tests/e2e/test_mcp_e2e.py:1-45` |
| 4 | `mypy` treats `agenthicc.tools.mcp` as untyped because the library is absent. | `pyproject.toml:136-147` |
| 5 | The defects found in MCP-02 (SSE served as Streamable HTTP, the dropped `env`/`cwd`, the always-failing first `call_tool`) are the kind of thing a real-client test catches on its first run. | MCP-02 rows 2, 3, 5 |
| 6 | `lauren-mcp`'s own guidance for testing a server is a hand-written stdio script that imports private modules, or Lauren's in-process `WsTestClient`/`TestClient`. Neither opens a real socket, so neither exercises agenthicc's client over HTTP or WebSocket. | `lauren-mcp/skills/mcp-testing/SKILL.md` patterns 1–2 |

## Goals

1. A real-client test can start a server over **stdio, Streamable HTTP, legacy SSE
   and WebSocket** with one fixture and no network beyond loopback.
2. The peer server is **independent of `lauren-mcp`'s server code**, so a defect in
   the library's client and its server cannot cancel out.
3. The peer can **misbehave on purpose** (paging, drops, slowness, malformed
   results, auth challenges), which no existing peer offers.
4. CI installs the client and runs the harness on every pull request, in the unit
   and integration jobs, within the existing time budgets.
5. A **conformance matrix** (capability × transport → test id) lives in the docs and
   a test fails if a row has no test.

## Non-goals

- A general MCP test framework for other projects.
- Replacing provider cassettes (`tests/conftest_cassette.py`); these tests exercise
  the MCP wire, not model calls.
- Running third-party reference servers in the required checks (see D5).

## Design

### D1 — The scenario server

`tests/fixtures/mcp/scenario_server.py`: one module, standard library plus
`aiohttp` (already a base dependency), that speaks the wire protocol directly and
reads a **scenario** (a small dataclass or JSON object) saying what it advertises
and how it behaves.

| Transport | How it is served |
|---|---|
| stdio | `python scenario_server.py --stdio --scenario FILE` (newline-delimited JSON-RPC) |
| Streamable HTTP | aiohttp app on `127.0.0.1:0`; POST with JSON or SSE body, `mcp-session-id`, DELETE teardown |
| Legacy SSE | aiohttp `GET /sse` event stream plus `POST /messages` |
| WebSocket | aiohttp websocket route |

A fixture `mcp_peer(transport, scenario, era="legacy")` takes an **era** of `legacy`,
`modern` or `dual`. The first release implements `legacy`; MCP-13 adds the other two
(`server/discover`, per-request `_meta`, the result envelope, `subscriptions/listen`,
multi round-trip requests), and the spec's compatibility matrix (client era × server era)
becomes the test table. The fixture returns an object with the connection
parameters (command or URL), a log of every frame received, and a way to inject
events (a `list_changed`, a `resources/updated`, a server request). It binds port 0,
waits on a readiness event (no `sleep`), and kills the process group on teardown.

### D2 — Scenarios, one per behaviour the other PRDs promise

| Scenario | Used by |
|---|---|
| `basic` — tools, one resource, one prompt, `instructions` | MCP-01, MCP-02 |
| `paged` — three pages of tools with `nextCursor` | MCP-02 U1, MCP-11 |
| `annotated` — `title`, `annotations`, `outputSchema`, prompt `arguments` | MCP-02 U2, MCP-06, MCP-08 |
| `slow_tool` — a tool that waits on a gate and records `notifications/cancelled` | MCP-04 |
| `progress` — emits `notifications/progress` for a given token and `notifications/message` logs | MCP-04 |
| `elicits`, `samples`, `asks_roots` — send the server-initiated requests | MCP-09 |
| `needs_auth` — answers `401` with `WWW-Authenticate` until a token is presented; serves discovery metadata | MCP-10 |
| `changing_catalog` — adds and removes tools, emits `list_changed` | MCP-06, MCP-11 |
| `many_tools` — hundreds of tools with long descriptions | MCP-11 |
| `crashy` — exits mid-call, closes the socket, returns invalid JSON, oversized result | TOOLR-06 (existing), MCP-04 |
| `dup_and_hostile` — duplicate names, huge schema, name collisions after sanitising | MCP-06 |
| `modern_only`, `dual_era` — answer `server/discover` and refuse or accept `initialize` | MCP-13 |

### D3 — A friendly peer from the same family

One test module runs `lauren-mcp`'s own server (`lmcp run server.py --transport
streamable`, available with the `[cli]` extra) for the happy path, to catch a
mismatch the scenario server's author shares with agenthicc's. It is the only place
the library's server code appears.

### D4 — CI and typing

- `dev` includes `agenthicc[mcp]` (MCP-01 D5); the mypy `ignore_missing_imports`
  entry for `agenthicc.tools.mcp` is removed.
- The harness tests are marked `integration` and sit in `tests/integration/mcp/`.
  Each starts its peer in under a second and finishes within `timeout = 60`.
- A guard test lists the matrix (D6) and fails when a capability row names a test
  that does not exist.

### D5 — Reference servers, optional

Keep the existing `AGENTHICC_MCP_E2E` tests, move them under `tests/e2e/mcp/`, add
the `server-everything` reference server, and run them in a **scheduled** workflow
(not a required check, because they need `npx` and the network). A failure opens an
issue; it never blocks a merge.

### D6 — The conformance matrix

`docs/reference/mcp-conformance.md` is generated from a table in
`tests/integration/mcp/matrix.py`: rows are capabilities (initialize, tools/list,
tools/call, pagination, list_changed, cancel, progress, logging, resources, templates,
prompts, completion, sampling, elicitation, roots, auth), columns are transports, a
cell is a test id or `n/a` with a reason. The docs build fails if the generated page
differs from the committed one.

## Failure handling

- A peer that fails to start fails the test with its stderr tail, not a timeout.
- A leaked server process fails the session: the fixture asserts no child is left.
- Under load, tests wait on events and use generous `wait_for` ceilings, never fixed
  sleeps (the repository already records timing flakes under load).

## Security

The peer binds loopback only and refuses non-loopback `Host` headers. Scenario files
hold no secrets; the `needs_auth` scenario uses a fixed test token.

## Rollout and migration

1. Land the `dev` extra and the mypy change (MCP-01 D5), fix what mypy now finds.
2. Add the scenario server, `basic` and `paged` scenarios, and one test per
   transport.
3. Each later MCP PRD adds its scenario and tests as part of its own change.
4. Add the scheduled reference-server workflow last.

## Acceptance criteria

1. On a clean checkout, `uv sync --extra dev` then `pytest tests/integration/mcp -q`
   connects over all four transports and lists tools from the `basic` scenario.
2. The `paged` scenario returns every tool once U1 (MCP-02) lands and, before that,
   a test asserts the documented truncation so the gap is explicit.
3. Reintroducing any MCP-02 defect (SSE → Streamable HTTP, `TypeError` fallback
   dropping `env`) fails a harness test.
4. `mypy src/agenthicc` passes with no override for `agenthicc.tools.mcp`.
5. The matrix page is generated, committed and checked in the docs build; a row
   without a test fails `pytest tests/integration/mcp/test_matrix.py`.
6. The harness adds no more than 30 seconds to the integration job.

## Verification

- Mutation: for each defect in MCP-02 rows 2, 3 and 5, restore the old code and
  confirm a test fails.
- Run the suite three times under `nice` load to confirm no sleep-based flake.
- `ps` shows no scenario server left after a failing test.

## Risks

- The scenario server is a second implementation of the protocol that the project
  must maintain. Its scope is limited to what agenthicc consumes, and D3 guards
  against the author's blind spots.
- Moving `mcp` into `dev` lengthens the install for contributors. It is three small
  packages (MCP-01 row 8).
