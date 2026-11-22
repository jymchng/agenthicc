---
title: "WFC-00: Workflow composition and agent self-extension — programme and study"
status: Proposed
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [prd-81, prd-114, prd-147, prd-154, prd-170, prd-213, prd-250, prd-258, SUBAGENT-04, SUBAGENT-12, SUBAGENT-15, TUIR-04]
tags: [workflows, subagents, tools, skills, reload, trust, programme]
---

# WFC-00 — Workflow composition and agent self-extension

## 1. What the programme delivers

Six outcomes, each a testable behaviour of a running session:

| # | Outcome | Owning PRDs |
|---|---|---|
| 1 | An agent enters a workflow; when that workflow completes, agenthicc returns the agent to the workflow it came from. | WFC-01, WFC-02, WFC-03 |
| 2 | An agent spawns subagents, and each subagent runs a named workflow. | WFC-04 |
| 3 | An agent activates `create_workflow`, completes it, and the session reloads so the new workflow is usable. | WFC-05, WFC-06, WFC-11, WFC-14 |
| 4 | An agent activates `create_tools` (renamed from `make_agenthicc_tool`), creates one or several tools, and the session reloads them so the agent can call them immediately. | WFC-05, WFC-07, WFC-08, WFC-11, WFC-14 |
| 5 | An agent activates the new `create_skills` workflow, creates one or more skills, and the session reloads them so the agent can use them. | WFC-05, WFC-09, WFC-10, WFC-11, WFC-14 |
| 6 | Related enhancements the above need to be safe and legible: durable stack, trust and provenance, catalog, operator visibility, an acceptance suite. | WFC-03, WFC-11, WFC-12, WFC-13, WFC-14 |

> **Naming.** The request spells the tool workflow `make_agenthicc_tools`. The
> shipped name is `make_agenthicc_tool` (singular;
> `workflows/loader.py:67`, `workflows/make_agenthicc_tool/`). WFC-07 renames
> the shipped workflow to `create_tools` and deletes the old name outright; no
> alias is registered.

## 2. Current-source findings

Measured against the tree at 2026-11-22. Line numbers are indicative; each PRD
re-verifies the ones it depends on before implementation.

| # | Finding | Where |
|---|---|---|
| F1 | **Workflow selection is a sticky single slot.** `TUISession._workflow_override: str \| None` is written only by `/workflow NAME`, `/workflow reset`, resume, and reload-removal. Nothing clears it when a run completes, so after `create_workflow` finishes the selector still names `create_workflow` and the next ordinary message starts another authoring run. | `runners/tui_session.py` (`:1819`, `:2052`, `:2077`, `:4250`, `:4547`); `_finalize_returned_workflow` at `:2145` closes the handle but never touches the selector |
| F2 | **One foreground run slot.** One `_workflow_handle` and one `_agent_task` per session. `WorkflowRunHandle` has no parent/child relation. | `tui_session.py:1716`; `runners/workflow_handle.py` |
| F3 | **No model-facing way to enter a workflow.** Entry is `/workflow` (a person) or the mode default. A search of `src/` for model tools that enter, switch or run a workflow found none. | `tui_session.py:5146` |
| F4 | **Workflows already run without a TUI.** `execute_workflow(session, workflow_name, intent, …)` builds the runner through `WorkflowPlugin.build_runner`. `spawn_subagents` accepts only `{type, task, context}`. | `runners/headless.py:209`; `subagents/tool.py:84` |
| F5 | **Reload is four unrelated operator methods.** `_reload_skills` (returns `SkillDiscoveryResult`), `_reload_tools` / `_reload_workflows` (return `tuple[bool, str]`), `_reload_commands`. They are injected into slash-command context only, run discovery synchronously on the event loop, publish no event, and do not exist headless. | `tui_session.py:3897-3900`, `:4115`, `:4179`, `:4230`, `:4262` |
| F6 | **Tools are snapshotted at four layers.** `WorkflowConfig` is frozen and `_reload_tools` swaps it with `dataclasses.replace`; `agent_turn` builds its registry from `ctx.project_plugin_tools` each turn; `CodePlanRunner` caches `_tool_bundle_cache`; `SubagentPool` keeps `_all_tools` from construction. A tool created mid-run is invisible to all four until each is rebuilt. | `workflows/config.py:56`; `runners/agent_turn.py:1466`; `workflows/code_plan/runner.py:1052-1067`; `subagents/pool.py:1462` |
| F7 | **`make_agenthicc_tool` is single-tool and ends with a manual step.** `MakeToolContext` holds one `tool_name`/`tool_file_path`; the finalize text tells the *user* to run `/tools reload`. A prompt-only bootstrap skill `create-tools` shares the surface name. | `workflows/make_agenthicc_tool/runner.py` (context ~`:120`, finalize ~`:758`); `skills/bootstrap.py:222` |
| F8 | **The two authoring workflows treat trust differently.** Only `~/.agenthicc` is trusted by location; any other Python extension needs a SHA-256 record in `~/.agenthicc/trust.json`, which the repository can never write. The loader returns `[]` for an untrusted package and skips a directory carrying the pending marker. The tool workflow writes into the live `.agenthicc/tools/` and never trusts, so its output does not load until the user runs `agenthicc trust`. `create_workflow` goes the other way: `finalize_authoring` records trust for every `.py` file of the validated package inside the runner, with a human decision only when *replacing* a workflow. A self-extending agent needs one in-session path for both, owned by the runtime. | `plugins/trust.py` (`ExtensionTrust.trust/check/permits/revoke`); `workflows/loader.py:122-140`; `workflows/create_workflow/authoring.py:1187-1216`; `create_workflow/runner.py:1071-1090` |
| F9 | **Skills are user-activated.** `$slug` sets `_pending_skill_body`; auto-matching runs on the *user's* text (`find_matching_skills(ctx.text, …)`). There is no model-facing skill tool and no `create_skills` workflow. | `tui_session.py:1888`; `runners/agent_turn.py:1413` |
| F10 | **A tool-boundary seam already exists.** `next_queued_message` is claimed at a completed tool boundary in the turn loop. It is the natural place to observe a changed tool surface. | `runners/agent_turn.py:1604`; `runners/agent_turn_context.py:157` |
| F11 | **The authoring contract exists once, in `create_workflow`, and not at all for tools.** `create_workflow` owns its verdict (the runner overrides an agent approval when deterministic validation failed), scopes generation to its lease, and bounds repair (`repair_cycles`). The tool workflow's verdict is the agent's `approve_tool`, its containment check is a substring test on a reported path, and its VALIDATE → GENERATE loop has no counter. Skills have no workflow. | `create_workflow/phase_tools.py:288-345`, `generation_tools.py:232`, `runner.py:530`, `:1725`; `make_agenthicc_tool/runner.py:62`, `:273`, `:290-342`, `:663-728` |

## 3. Architecture overview

```text
                    ┌──────────────────── TUISession / headless ────────────────────┐
user /workflow ───▶ │ WorkflowStack  (WFC-01)          ReloadService (WFC-05)        │
agent enter_workflow│  base ▸ frame ▸ frame            tools · skills · workflows ·  │
 (WFC-02)       ──▶ │  pop ⇒ WorkflowReturn            commands → ReloadReport       │
                    │        │                                ▲   ▲                  │
                    │        ▼                                │   │                  │
                    │ return delivery ── publish step (WFC-11)┘   └─ ToolSurface     │
                    │  (synthetic turn)   gate · trust · provenance     (WFC-08)     │
                    └───────┬──────────────────────────────────────────────────────┬─┘
                            │ SubagentPool (WFC-04)                                │
                            ▼   task{type, task, workflow, params}                 ▼
                   WorkflowWorker → run_workflow_core → WorkflowReturn      use_skill / list_skills
                   (own handle, own worktree, staged artifacts)             (WFC-10)
```

Three rules hold the design together:

1. **Authoring workflows stage; the session publishes.** A workflow (or a
   subagent running one) writes candidates to a staging area and returns a
   typed `WorkflowReturn` listing `PublishedArtifact`s. Only the session —
   automatically, with no human approval, once the runner-owned verdict has
   passed — moves files into live directories, records trust, and reloads
   (WFC-11, WFC-05).
2. **Return is an event, not a convention.** Completion pops the stack, runs
   the publish/reload step, then delivers exactly one return packet to the
   parent as a synthetic turn (WFC-01, WFC-03).
3. **Surfaces are generation-versioned snapshots.** Tools, skills, workflows
   and commands each have a current immutable surface and a generation
   number; consumers read the current surface at a defined boundary instead of
   keeping a private copy (WFC-05, WFC-08).

## 4. Shared vocabulary

| Term | Meaning |
|---|---|
| **Base frame** | The workflow chosen by mode default or `/workflow NAME`. Replaces `_workflow_override`. |
| **Call frame** | A workflow entered on top of the base (by a person or an agent) that returns when it completes. |
| **`WorkflowReturn`** | Bounded, typed result a completed frame delivers to its parent (WFC-01). |
| **`PublishedArtifact`** | One tool, skill, workflow or command an authoring run produced, with digest and state (WFC-11). |
| **`ReloadReport`** | Typed outcome of reloading one or more surfaces (WFC-05). |
| **Generation** | Monotonic integer a `ReloadService` bumps each time any surface changes. |
| **Staging** | `.agenthicc/.staging/<run_id>/…`, inside `WorkspaceView`, never scanned by any loader. |

## 5. Dependencies and delivery order

### 5.1 What each PRD needs

| PRD | Needs | Notes |
|---|---|---|
| WFC-14 authoring contract | — | Its sandboxed import harness (§5.5) uses WFC-11 §5.5; land the rest first and wire the harness when WFC-11 lands. |
| WFC-05 reload service | — | Also defines the immutable-surface holder that WFC-08 and WFC-10 reuse. |
| WFC-10 skill tools | WFC-05 | Makes *existing* skills model-invocable; useful on its own. |
| WFC-11 publish, trust, provenance | WFC-05, WFC-14 | Its trigger (a popped `WorkflowReturn`) needs WFC-01 — see the seam below. |
| WFC-01 workflow stack | the `PublishedArtifact` type from WFC-11 | Used only as a field type of `WorkflowReturn`. |
| WFC-02 enter tools | WFC-01 | |
| WFC-06 `create_workflow` | WFC-01, 02, 05, 11, 14 | Smallest authoring delta: the lease machinery already exists. |
| WFC-08 live tool surface | WFC-05 | |
| WFC-07 `create_tools` | WFC-01, 02, 05, 08, 11, 14 | |
| WFC-09 `create_skills` | WFC-01, 02, 05, 10, 11, 14 | |
| WFC-04 subagent workflows | WFC-02, 11, 14 | Most useful once an authoring workflow exists to run. |
| WFC-03 durable stack | WFC-01, 02 | Schema and event changes; do before the stack is widely used. |
| WFC-12 catalog and visibility | lands in pieces | Breadcrumb and blocks with WFC-01, publish notice with WFC-11, catalog after WFC-02/10/11. |
| WFC-13 acceptance suite | harness first | Each PRD ships its own scenario; WFC-13 closes the series. |

**The seam.** WFC-11 is triggered by a popped `WorkflowReturn` (WFC-01), and
`WorkflowReturn` carries `PublishedArtifact`s (WFC-11). Break the cycle by
building WFC-11's types, store, publish step, provenance and `/authored`
commands first and testing them by calling the publish step directly; build
WFC-01 next; connect "pop → publish step" as the last slice of WFC-01. No stub
or interim trigger is added.

### 5.2 Delivery order

| Step | PRDs | Why here | Unlocks |
|---|---|---|---|
| 0 | Three spikes, no production code: lauren-ai tool-list swap between iterations (WFC-08 §5.2); the `dict[str, str] \| None` tool schema (WFC-02 §5.2, WFC-04 §5.1); a Bubblewrap-sandboxed import harness (WFC-11 §5.5) | Each decides a design branch cheaply before it is built on. | — |
| 1 | WFC-14 ‖ WFC-05 ‖ WFC-13 harness | No dependencies; everything else needs them. | — |
| 2 | WFC-11 ‖ WFC-10 | The publish step and its trust call-site consolidation; skill tools ride on WFC-05. | Existing skills usable by the agent |
| 3 | WFC-01 (+ the pop → publish connection) | The stack and return delivery. | Return to the previous workflow (stub workflows) |
| 4 | WFC-02 | The model-facing entry tools and `WorkflowInvocation`. | **Outcome 1** |
| 5 | WFC-06 | Smallest authoring change; proves stack → publish → reload → return end to end. | **Outcome 3** |
| 6 | WFC-08, then WFC-07 | The live tool surface, then the workflow that needs it. | **Outcome 4** |
| 7 | WFC-09 | `create_skills`, on top of WFC-10 and the same publish path. | **Outcome 5** |
| 8 | WFC-04 ‖ WFC-03 | Subagent workflows (worker import path exercised by real authoring workflows); durable stack and replay. | **Outcome 2**; crash-safe return |
| 9 | WFC-12 (catalog, remaining visibility), WFC-13 (traceability, S9 performance gate) | Close-out. | Series exit (§8) |

Parallel work inside a step is safe: the PRDs touch different modules
(`workflows/authoring/` vs `runners/reload.py`; `plugins/authored.py` vs
`skills/tools.py`; `subagents/pool.py` vs kernel and checkpoints). The critical
path is WFC-05 → WFC-11 → WFC-01 → WFC-02 → WFC-06 → WFC-08 → WFC-07.

## 6. Cross-cutting rules (apply to every WFC PRD)

- **No legacy.** Each PRD names the symbols it supersedes and deletes them in the
  same change. No alias, shim, `Optional` guard or dual path
  (`_workflow_override`, `_reload_*`, `MakeTool*`, the `create-tools` bootstrap
  skill and the per-layer tool snapshots are all deleted by their owners).
  A repository-wide search for each old name must return zero hits.
- **Types.** New or modified signatures use concrete parameterized types.
  `typing.Any` is not used; where a payload is open it is `object` narrowed at
  the boundary, or a `TypedDict`/frozen dataclass.
- **Two state containers.** Frames and reload generations are domain facts
  (kernel `AppState`, changed only by events through `root_reducer`). The
  breadcrumb, notices and overlays are presentation state in
  `tui/conversation_store.py`. The bridge lives in `runners/tui_session.py` and
  is tested (WFC-03).
- **Capabilities only narrow.** A child frame, a workflow subagent, a model-invoked
  skill and an authored tool each run under the intersection of their parent's
  ceiling and their own declaration.
- **The runtime decides trust, automatically.** Authored extensions are trusted
  without a human approval (decision 2026-11-22). Trust is recorded only by the
  session's publish step, only for bytes that passed runner-owned validation,
  and never by a model-facing tool (WFC-11).
- **No new subagent limits.** Subagents run to completion under the existing
  session turn limit (SUBAGENT-12 and the removal of subagent limits). WFC-04
  adds none. The only new bounds are foreground-frame recursion guards (WFC-01),
  which protect the single interactive slot.
- **Docs in the same change.** Each PRD lists the guide and `llms-full.txt`
  entries it changes; `uv run mkdocs build --strict` and
  `uv run nox -s llms_check` pass.
- **Test placement.** Unit for pure stack/report/validation logic; integration
  for processor, trust store and reload against real files; E2E for cassette
  scenarios; performance for event-loop lag during reload (TUIR-04 budget).

## 7. Open decisions and recommendations

| Decision | Recommendation | Owner |
|---|---|---|
| May a phased workflow (`code_plan`) call `enter_workflow` from inside a phase? | No in v1. Only free-form agent frames (and workflows that opt in with `allows_child_workflows`) may enter children; phased workflows remain valid *children*. | WFC-01/02 |
| Is a human approval required to trust authored workflows, tools and skills? | **No — decided 2026-11-22.** The session trusts them automatically after the runner-owned verdict; operators may opt into `stage_only`. The controls that replace the gate are in WFC-11 §5.4. The trust call moves from the `create_workflow` runner to the session's publish step (WFC-06 §5.2). | WFC-06/11 |
| Interactive (human-gated) workflows as subagents? | Not in v1; fail closed with a typed error. Routing gates to the parent through `subagents/communication.py` is a follow-up. | WFC-04 |
| Same-turn availability of a new tool? | Guarantee the first model request after the return packet; same-turn pickup at a tool boundary is a spike with a defined fallback. | WFC-08 |
| Rename `SubagentExecutionPolicy` (it now also snapshots frame policy)? | Out of scope; reuse as is. | WFC-01 |

## 8. Series exit criteria

The series is complete when WFC-13's scenarios S1–S9 pass in CI, every PRD's
acceptance table maps to a named test, the legacy-symbol searches return zero
hits, and the documentation gates pass.
