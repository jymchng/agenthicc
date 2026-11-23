---
title: "WFC-13: End-to-end acceptance suite for workflow composition and self-extension"
status: Implemented
version: 1.0.0
date: 2026-11-22
repository: jymchng/agenthicc
priority: P0
related_prds: [WFC-00, WFC-01, WFC-02, WFC-03, WFC-04, WFC-05, WFC-06, WFC-07, WFC-08, WFC-09, WFC-10, WFC-11, WFC-12, WFC-14, TUIR-04]
tags: [testing, e2e, cassettes, acceptance, performance]
---

# WFC-13 — End-to-end acceptance suite

## 1. Summary

Unit and integration tests prove each piece. The six outcomes of
[WFC-00](WFC-00-programme-and-study.md) are only true if the pieces compose in
a real session: stack, tools, reload, trust, surface and skills together. This
PRD defines nine scenario tests, the harness they share, a traceability table
that ties every WFC acceptance criterion to a named test, and the CI gate that
closes the series.

## 2. Problem

- The pieces cross four boundaries that no single existing test crosses:
  turn → workflow frame → publication/trust → reload → *next model request*.
  A regression in any seam (a stale tool snapshot, a return delivered twice, a
  skill dict observed empty) passes every unit test.
- `tests/conftest_cassette.py` and `tests/e2e/` provide cassette transports and
  full-session fixtures, but none drives a session through an authoring
  workflow and asserts what the *model request* contained afterwards.
- Reload introduces a new way to stall the event loop (TUIR-00 measured a
  93.7 s stall from filesystem work on the loop); nothing budgets it.

## 3. Goals

1. One scenario per outcome, runnable offline against scripted transports.
2. Assertions on **observable model requests**, not only internal state.
3. A traceability table so no PRD criterion is untested.
4. A loop-lag budget for reload in CI.

## 4. Non-goals

- Live-provider tests in CI. A manual `--live` marker may exist for maintainers
  and is excluded from required checks.
- Browser/UI screenshot tests beyond existing TUI rendering tests.

## 5. Harness

`tests/e2e/self_extension/` with:

| Fixture | Provides |
|---|---|
| `authored_session` | A real `SessionContext` + `EventProcessor` + `ReloadService` + `ExtensionTrust` pointed at `tmp_path` (`override_extension_trust`), a scripted transport, and a headless or TUI driver. |
| `script` | A cassette-style scripted model: a list of `(match request → reply/tool_calls)` steps that **fails the test** on an unexpected request. |
| `request_log` | Every model request (system blocks, tool names, messages) for assertions about tool lists, catalog text and return blocks. |
| `policy` | Selects `security.authored_extension_policy` (`auto`, the default, or `stage_only`) and, for `stage_only`, drives `/authored approve` and `reject`. |
| `crash_at` | Injects a process stop at a named seam (after pop, after enqueue, mid-publish). |

Rules: no network, no real clock sleeps (inject the clock), `bwrap` required on
Linux CI as already documented; on platforms without it the sandbox-specific
assertion is skipped with a reason, and the `static_only` path is asserted
instead.

## 6. Scenarios

| ID | Outcome | Steps | Key assertions |
|---|---|---|---|
| **S1** | 1 | `default` agent → `enter_workflow` a stub two-phase workflow → completes → parent continues. | Child starts only after the turn ends; stack depth returns to 0; the next user message runs `default`; the parent's resume request contains exactly one return block. |
| **S2** | 2 | `spawn_subagents` with two tasks, `workflow="create_tools"`, different tools. | Two worktrees, two staged sets, nothing in the main `.agenthicc/tools/`; digest carries both `WorkflowReturn`s; an `interactive` workflow task fails typed while its sibling succeeds. |
| **S3** | 3 | `enter_workflow("create_workflow")` → run completes with no prompt → agent enters the new workflow. | Registry gained the name, trusted, before the resume turn; the agent's next `enter_workflow(<new>)` succeeds with no manual reload; a run whose validation fails leaves the registry unchanged and nothing trusted. |
| **S4** | 4 | `enter_workflow("create_tools")` planning three tools → run completes with no prompt → the agent calls one. | The resume request's tool list contains all three; the call returns the tool's result; a worker spawned after sees them, one spawned before does not; `grep` finds no `make_agenthicc_tool`. |
| **S5** | 5 | `enter_workflow("create_skills")` two skills → run completes with no prompt → `list_skills` → `use_skill`. | Both skills in the live surface; wrapped body in the tool result; `$slug` commands in the picker; a skill with `disabled_tools` narrows the next request in the same turn. |
| **S6** | 6 | Trust integrity and `stage_only`. | A failing verdict commits and trusts nothing; under `stage_only` nothing loads until `/authored approve`, and `reject` leaves no live file, no trust record and no manifest entry; a hand-edited published tool reports `untrusted_modified` and leaves the surface; a static scan proves no tool module can mutate trust and no authoring path can write under `~/.agenthicc`; a published `execute` tool still hits the Safe-mode approval prompt on each call. |
| **S7** | 1, 6 | Crash after pop and before delivery; separately, crash mid-publish of a three-artifact set. | Restart delivers exactly one parent resume turn; a mid-publish crash leaves the previous state (all-or-nothing) and recoverable staging. |
| **S8** | 6 | Headless session: default policy, then `stage_only`. | Default: artifacts are committed, trusted and loaded with no prompt and one `authored_published` JSON line is emitted. `stage_only`: they stay `staged` until `agenthicc authored approve <run-id>`, and the next reload loads them. |
| **S9** | 1, 6 | Guards and budgets. | Depth 5 and a repeated name are rejected without creating a run; reload with 200 tool files and 50 skills keeps worst loop stall < 50 ms and p95 < 10 ms (performance test, TUIR-04 sampler). |

## 7. Traceability

Every row below gets a test id in the implementing change; the series is not
complete while any cell is empty.

| PRD | Criteria | Primary scenario | Primary test |
| --- | --- | --- | --- |
| WFC-01 | A1–A8 | S1, S9 | `tests/e2e/self_extension/test_s01_workflow_call_return.py::test_two_child_phases_return_once_to_the_parent` |
| WFC-02 | A1–A6 | S1, S9 | `tests/integration/test_enter_workflow_handoff.py::test_opted_in_parent_resumes_the_same_checkpoint_after_child` |
| WFC-03 | A1–A6 | S7 | `tests/e2e/self_extension/test_s07_durable_return.py::test_restart_delivers_one_parent_turn_after_durable_return` |
| WFC-04 | A1–A7 | S2 | `tests/e2e/self_extension/test_s02_workflow_workers.py::test_parallel_tool_authors_keep_their_staging_separate` |
| WFC-05 | A1–A7 | S4, S9 | `tests/integration/test_reload_service_surfaces.py::test_tools_publish_healthy_files_and_preserve_failed_surfaces` |
| WFC-06 | A1–A11 | S3 | `tests/e2e/self_extension/test_s03_create_workflow.py::test_new_workflow_is_published_and_called_in_the_same_session` |
| WFC-07 | A1–A8 | S4 | `tests/e2e/self_extension/test_s04_create_tools.py::test_create_tools_publishes_and_dispatches_on_the_first_parent_request` |
| WFC-08 | A1–A7 | S4 | `tests/integration/test_live_tool_surface.py::test_direct_turn_uses_new_schema_and_dispatch_after_reload` |
| WFC-09 | A1–A7 | S5 | `tests/e2e/self_extension/test_s05_create_skills.py::test_new_skills_are_discovered_and_used_on_the_first_parent_turn` |
| WFC-10 | A1–A8 | S5 | `tests/integration/test_use_skill.py::test_model_uses_authored_skill_and_worker_inherits_remaining_tools` |
| WFC-11 | A1–A14 | S4, S6, S7, S8 | `tests/e2e/self_extension/test_s08_headless_publish.py::test_headless_publication_and_cli_approval` |
| WFC-12 | A1–A8 | S1, S3–S5 | `tests/integration/test_workflow_stack_session.py::test_catalog_command_matches_the_model_and_survives_an_unchanged_reload` |
| WFC-14 | A1–A8 | S4, S5, S6 | `tests/integration/test_authoring_contract.py::test_checkpoint_restart_preserves_repairs_and_exhausts_the_original_budget` |

Each linked PRD records the rest of its criterion-level evidence.

## 8. Gates

- The nine scenarios run in `nox -s tests_e2e` and are marked `e2e`; S9's
  performance half runs in the existing E2E-and-performance CI job.
- `uv run ruff check`, `ruff format --check`, `mypy src/agenthicc`, and the type
  audit are unchanged or improved; no `Any` in any new module.
- `uv run mkdocs build --strict` and `uv run nox -s llms_check` pass.
- **Legacy-symbol searches** (zero hits outside historical PRDs/CHANGELOG):
  `_workflow_override`, `workflow_override`, `_reload_skills`, `_reload_tools`,
  `_reload_workflows`, `_reload_commands`, `make_agenthicc_tool`, `MakeTool`,
  `submit_tool_plan`, `_tool_bundle_cache`, `_max_repair_cycles`, `confirm_generation_complete`, `project_plugin_tools` as a private
  snapshot, and the `create-tools` / `create-workflow` bootstrap skills.
- Release notes in `CHANGELOG.md` mark the rename and the removed commands as
  breaking.

## 9. Acceptance criteria

| # | Criterion |
|---|---|
| A1 | S1–S9 pass in CI on Linux; S2, S4 and S9 run on every supported Python version in the existing matrix. |
| A2 | Mutation check: reverting the generation read in `agent_turn` (WFC-08) fails S4; removing the idempotency key (WFC-03) fails S7; making the trust call reachable from a tool fails S6. |
| A3 | The traceability table has no empty cell and every referenced test exists. |
| A4 | Each scenario asserts on a captured model request at least once. |
| A5 | The suite adds < 90 s to the E2E job and uses no wall-clock sleeps. |

## 10. Risks

| Risk | Mitigation |
|---|---|
| Scripted transports hide a real-provider behaviour (tool-list swap). | The WFC-08 spike test and an optional maintainer `--live` marker; the guarantee in WFC-08 §5.3 does not depend on the spike. |
| Platform differences (sandbox, worktrees). | Explicit skips with reasons and the `static_only` path asserted on the skipped platforms. |
| Scenario drift from the PRDs. | The traceability table is the review checklist for every WFC PR. |

## Amendment — 2026-11-22

The legacy scan holds recorded cassette history and the schema-1 rename
diagnostic outside the runtime-symbol check. S7 runs its abrupt termination
cases with real subprocesses. The publisher crash case interrupts a file move
inside a three-artifact transaction. The S4 same-turn check uses the integration
fixture for a reload arriving between two provider requests.

## Implementation status

| Criterion | Evidence |
| --- | --- |
| A1 | The E2E session runs S1–S9. The integration matrix runs S2, S4, and S9 on Python 3.11, 3.12, and 3.13. |
| A2 | The same-turn generation test, S7 subprocess test, and trust-boundary AST test are the mutation checks. |
| A3 | The traceability table uses collected test names and the PRDs carry the criterion-level evidence. |
| A4 | The scripted S1–S9 fixtures check the provider request or resume request. The static, subprocess, and performance cases supplement those requests. |
| A5 | The offline scenarios use task barriers and durable receipts. The final E2E duration is recorded in the implementation log. |
