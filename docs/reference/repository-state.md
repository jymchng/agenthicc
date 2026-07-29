# Current repository state

This is the maintainer-facing state audit for the checkout at commit
`4052c66` (29 July 2026). It records what is implemented, what is only a
compatibility boundary, and what remains roadmap work. It is intentionally
separate from historical PRDs: a PRD can describe a proposed design without
being a description of the running package.

## Evidence snapshot

The audit used the current source tree, package metadata, Nox sessions, and
the test layout as its evidence. The checkout contains:

| Area | Current evidence | Meaning |
|---|---:|---|
| Python source files | 208 | Broad runtime and integration surface under `src/agenthicc/` |
| Python test files | 195 | Unit, integration, and E2E coverage |
| Markdown docs | 29 | User, contributor, architecture, guide, and reference docs |
| PRDs/research docs | 156 | Historical and proposed product/design records |
| Package version | `0.1.0` | Still hard-coded in `pyproject.toml` |
| Supported Python | `>=3.11` | Nox exercises 3.12 and 3.13 |
| Declared extras | `cloud`, `dev` | There are no separate `tui`, `api`, or `all` extras |

The full local verification baseline currently passes with 2,754 tests passed
and 15 skipped. That proves the checked-in test contracts, not that every
roadmap concern below is solved.

## Supported runtime surfaces

The supported product path is:

```text
agenthicc CLI
   ├─ interactive TUISession
   │    └─ Rich Live Workspace + reactive presentation state
   ├─ --headless stdin runner → JSON-lines
   └─ session commands → client-neutral SessionService
                         └─ optional loopback HTTP/SSE adapter
```

The event-sourced kernel, workflow runners, tool/capability path, memory
layers, and durable journals sit behind those entry points. The loopback
session transport is an adapter over the in-process service; it is not the
historical `agenthicc.api` server and it does not start automatically.

### Authoritative ownership boundaries

| Concern | Authoritative implementation | Durable or ephemeral |
|---|---|---|
| Domain state | `kernel/state.py` frozen `kernel.AppState` | Durable/replayable through events |
| Domain transitions | `kernel/events.py` and `kernel/reducer.py` | Pure reduction |
| Event processing | `kernel/processor.py` | Queue, persistence, effects, subscribers |
| Session construction | `runners/session_context.py` | Owns runtime resources |
| Interactive orchestration | `runners/tui_session.py` | Session lifecycle and UI bridge |
| Headless input | `runners/headless.py` | Stdin/kernel smoke and workflow entry points |
| Reactive presentation | `tui/conversation_store.py` | Ephemeral UI/input state |
| Rich rendering | `tui/workspace/` | Ephemeral terminal presentation |
| Workflow execution | `workflows/` | Phase state and handoff context |
| Tool policy and approvals | `tools/capabilities.py`, `tools/capability_gate.py`, `tools/approval.py` | Runtime authorization |
| Tool adapter | `tools/executor.py`, `tools/hooks.py` | Lauren-ai compatibility boundary |
| Memory and journals | `memory/`, `tui/runtime/`, `tools/fs/file_cache.py` | Tiered and session durability |
| Client-neutral projection | `session_service/` | Durable event projection plus bounded subscriptions |

There are two `AppState` types by design. Kernel state must change through
events and the pure reducer. Reactive TUI state owns terminal-only concerns.
When a feature crosses that boundary, the bridge belongs in the session/runner
layer and needs both event and presentation tests.

## Workflow reality

The built-in workflows have specialized runners:

- `code_plan` uses `CodePlanRunner` and a typed `CodePlanState` loop for
  `plan → execute → review → summarize`.
- `create_workflow` uses its own typed authoring state, phase artifacts, direct
  source generation, deterministic validation, and resume/retry rules.
- Generic `WorkflowRunner` executes declarative `PhaseSpec` graphs and supports
  model overrides, command gates, human phases, parallel phases, and resume.

`PhaseSpec` is not the sole source of truth for the specialized `code_plan` and
`create_workflow` runners. Their class-level phase metadata is used for
registry/UI/configuration surfaces, while the specialized runner owns prompts,
phase loops, and transition tools. This remains an architectural improvement
item: either reconcile the two representations or document the split wherever
phase metadata is consumed.

All workflow transitions that matter to correctness are tool-controlled. The
runner checks an event and structured handoff data after the agent turn rather
than inferring a transition from prose. Approval, rejection, retry, command
failure, and resume behavior are distinct contracts and should not be collapsed
into a single `approved` truth value.

## Extension and trust model

The current extension surfaces are separate registries/loaders for workflows,
agents, tools, commands, skills, modes, plugins, and MCP servers. Project-local
Python extensions remain executable code. Discovery, trust, dependency
installation, shadowing, and headless behavior therefore remain security
boundaries; new documentation must not imply that a discovered plugin is safe
merely because it was found.

`tools/hooks.py` and `tools/executor.py` are real modules, but they are thin
adapters over lauren-ai's canonical `ToolHook`, decision objects, and executor.
They are not the removed standalone lifecycle engine described by older PRDs.
Use `docs/guides/hooks.md` for the supported boundary.

## Persistence and recovery

Persistence is split by owner rather than stored as one universal event log:

- kernel events and snapshots record domain state;
- conversation events and the journal support UI history and interrupted-turn
  recovery;
- project/global memory stores routed values and artifacts;
- the workspace file cache stores freshness-checked file content;
- cassettes record transport and approval interactions for deterministic replay;
- the session service stores client-neutral snapshots, durable event cursors,
  idempotency records, and bounded subscription state.

See [the storage reference](storage.md) before adding a file, retention policy,
or resume format. A new durable field needs corruption/restart coverage and a
documented owner.

## Documentation and release-gate findings

The maintained README and guides now describe the Rich TUI, headless stdin
interface, session service, three modes, current workflow authoring path, and
the absent historical API explicitly. The following remain open and are tracked
by PRD-138:

1. `llms-full.txt` is checked for headings by an embedded Nox script, but there
   is no source-to-reference generator or complete stale-section verifier.
2. MkDocs is not declared in `pyproject.toml`, and there is no default Nox docs
   build/link-check session. A clean checkout cannot claim a reproducible docs
   release gate until P0.5 is completed.
3. The package and CLI version are maintained independently; release metadata
   can drift from `pyproject.toml`.
4. The workflow findings in
   [`workflow-review.md`](workflow-review.md) need code-level revalidation and
   status updates rather than being treated as current bugs by default.
5. PRD-138 P0.2 still owns the decision to implement a supported server API or
   remove compatibility-only API configuration and historical references.

## How to use this document

Use this page to choose the ownership boundary before changing code. Use the
user guides for supported behavior, `llms-full.txt` for AI-facing public
symbols, and PRDs for proposals or historical decisions. If those sources
disagree, verify the source tree and update this audit plus the affected
maintained documentation in the same change.
