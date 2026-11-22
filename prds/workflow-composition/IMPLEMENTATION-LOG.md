# Workflow composition implementation log

Branch `feat/workflow-composition` starts at `5100c8c` from fetched
`origin/main`. The dedicated worktree is `/root/python_projects/agenthicc-wfc`.

| Step | Status | Commit and evidence |
|---|---|---|
| Setup | Complete | `4deacb1` adds the series and its index block. Parsed all 15 front-matter blocks and resolved the series links. `2b9902a` applies the separate guidance patch. Hooks installed. Strict MkDocs, Ruff and mypy passed. |
| 0 | M0 pushed | `a04bfb4` records three spikes and four amendments. The tool-surface follow-up also demonstrates dispatch after a same-turn schema refresh. See milestone gate results below. |
| 1 | M1 pushed | WFC-14 foundation, WFC-05 and the WFC-13 harness landed. Full local gate and pre-push checks passed. The remote tip matched `aa2bac1`. |
| 2 | In progress | WFC-11 publication service, operator controls and lease handling implemented. WFC-10 is next. |
| 3 | Pending | WFC-01 and the publish connection. |
| 4 | Pending | WFC-02. |
| 5 | Pending | WFC-06. |
| 6 | Pending | WFC-08 and WFC-07. |
| 7 | Pending | WFC-09. |
| 8 | Pending | WFC-04 and WFC-03. |
| 9 | Pending | WFC-12 and WFC-13 close-out. |

| PRD | Status | Commit and evidence |
|---|---|---|
| WFC-01 | Pending | — |
| WFC-02 | Spike complete | Optional string mapping confirmed. |
| WFC-03 | Pending | — |
| WFC-04 | Spike complete | Nested mapping confirmed with a correction to optional keys in the published schema. |
| WFC-05 | Implemented | `c757516`, `7bd642c`. Both runners use the service. See the PRD for A1–A7 evidence. |
| WFC-06 | Pending | — |
| WFC-07 | Pending | — |
| WFC-08 | Spike complete | Same-turn refresh verified by updating the provider schema list and the dispatch map together. |
| WFC-09 | Pending | — |
| WFC-10 | Pending | — |
| WFC-11 | Runtime ready | `6958a4b` isolates validation and smoke. `59e893a` adds publication, operator controls and recovery. The canonical lease uses the same store. The workflow return connection and downstream authoring scenarios remain in their scheduled steps. |
| WFC-12 | Pending | — |
| WFC-13 | Harness ready | `7536d33` adds a scripted provider, request snapshots, isolated real sessions, and a test-only failure trigger. S1–S9 remain to be integrated. |
| WFC-14 | Foundation | Shared scoped tools, deterministic verdict and persisted budget adopted by `create_workflow`. Sandbox validation and tool and skill adoption follow WFC-11, WFC-07 and WFC-09. |

## M0 verification

Initial test invocation used the login user's pytest temporary directory and
failed ownership checks. Re-runs use a dedicated `PYTEST_DEBUG_TEMPROOT`.
The first sandbox probe established that `--disable-userns` requires an
explicit `--unshare-user` flag. The first nested schema probe exposed the raw
Lauren required-key behavior now captured by the schema spike.

- All nine targeted spike cases passed. Ruff check and format, mypy (411
  source files), type-audit (10 metrics), and strict MkDocs passed.
- Full suites passed on the spike tree with 6,213 unit tests and 15 skips on
  Python 3.13, 382 integration tests on Python 3.14, and 235 E2E tests and
  two skips on Python 3.13.
- The initial unit suite had three environment-sensitive failures from the
  inherited no-color setting and the length of the test workspace path. The
  isolated rerun and the full rerun both passed with `NO_COLOR` unset,
  `TERM=xterm-256color`, and `PYTEST_DEBUG_TEMPROOT=/var/tmp/wfc`.
- The concurrent Python 3.13 integration run hit the existing subagent-panel
  duration assertion (`0.1s` versus `0.0s`). Checked host load and reran
  `test_provider_requests_and_the_result_are_the_same_without_the_panel`
  in isolation on 3.13. It passed. The full Python 3.14 integration recheck
  passed after the provider-schema follow-up test was added.

The first branch push ended at `8580289`. Its pre-push hygiene, mypy, type-audit
and unit checks passed. The remote tracking ref matched the local commit.

## WFC-14 foundation

- A1 uses `test_e2e_a_wrong_approval_cannot_finish_the_run` and
  `test_runner_verdict_requires_a_clean_report_and_agent_approval`.
- A2 uses `test_scoped_writer_reports_each_rejection`,
  `test_scoped_writer_rejects_symlink` and
  `test_concurrent_scoped_writes_share_the_file_limit`.
- A3 uses `test_e2e_run_fails_when_the_repair_budget_is_exhausted`.
- A4 uses `test_checkpoint_restart_preserves_repairs_and_exhausts_the_original_budget`
  and `test_authoring_checkpoint_requires_persisted_budget`.
- A5 uses `test_edits_after_approval_require_a_fresh_workflow_verdict`.
- A6 is pending the WFC-11 importer. A7 and A8 need the WFC-07 and WFC-09
  workflows for their cross-workflow checks.
- The targeted authoring, checkpoint, integration and E2E selection passed
  277 tests. Ruff check and format, mypy (414 files), the type-safety audit,
  strict MkDocs and `llms_check` passed. Searches found no previous scoped
  writer symbols or private repair counter in active code and documentation.

## WFC-05 service and event

The service, real filesystem integration, kernel contract, and lag selection
passed 10 tests. `test_reload_discovering_two_hundred_tools_and_fifty_skills_meets_lag_budget`
used the TUIR monitor with the specified 50 ms maximum and 10 ms p95 bounds.
Session construction, command migration and complete WFC-05 acceptance remain
in step 1.
A concurrent recheck alongside the documentation gates measured 42.2 ms p95.
After checking host load, the same performance test passed in isolation
without a budget change. Ruff, mypy, the type-safety audit, strict MkDocs, and
`llms_check` passed.

The WFC-05 runtime selection passed 733 tests on Python 3.13. The isolated
200-tool and 50-skill benchmark passed again after the runtime integration.
The legacy names test, Ruff, mypy, the type-safety audit, strict MkDocs and
`llms_check` passed.

Whole-repository combined line and branch coverage must stay above 80%.
The existing 81.5% CI floor is kept. Series-wide coverage evidence is pending
the full run.

## WFC-13 harness

The harness exercises the real headless execution path and copies provider
requests before tools change. It handles both message forms sent through the
provider interface. These three tests passed on Python 3.13:

- `test_harness_captures_the_real_headless_workflow_request`
- `test_harness_only_stops_at_the_armed_boundary`
- `test_script_refuses_an_unexpected_request`

Ruff and mypy passed across the source and harness. The failure trigger is
test-only. S7 still needs a real subprocess crash and restart scenario.

## M1 verification

The initial full unit suite reported 8 failures, 6,228 passes and 15 skips.
The background command fixture lacked the session reload service. Two busy
command tests found that older reload actions had moved out of the queue.
The checkpoint codec reported the missing budget before the invalid state.
One runtime mode assertion still required a Yolo override and three picker
assertions omitted the new `/workflow reload` action. The log is
`/var/tmp/agenthicc-wfc-m1-unit.log`.

The background fixture now uses the shared session setup. The legacy actions
keep their queue behavior and the dedicated reload command runs during an
active turn. Malformed checkpoint state takes precedence over the repair
budget check. The mode and picker tests cover the intended behavior. These
corrections passed 78 targeted tests, Ruff and mypy. The full recheck is
complete.

The corrected full gate passed on Python 3.13:

- 6,236 unit tests and 15 skips, 386 integration tests, and 238 E2E tests and two skips.
- Nine performance tests, including the reload lag budget.
- Ruff check and format, mypy on 416 source files, and the 10-metric type audit.
- Strict MkDocs and the public-symbol documentation check.

The logs are `/var/tmp/agenthicc-wfc-m1-final-*.log`. Full line and branch
coverage will be measured by the PR coverage job.

## WFC-11 workflow validation boundary

The first two integration cases reproduced in-process execution and missing
static fallback against the M1 tree. The sandboxed implementation passed the
244-test workflow selection, then all seven isolation cases. The production
sandbox tests prove that candidate imports run without the parent's environment,
with a private network and read-only workspace, and that local relative imports
stay available during inspection. They also verify time and output limits and
the `static_only` fallback. The publish receipt still needs to carry the
validation status to satisfy WFC-11 A14 end to end.

Ruff, mypy on 417 source files, type audit, strict MkDocs and `llms_check`
passed. The audit baselines decreased for explicit `Any` and `getattr` usage.

## WFC-11 authored publication service

The focused selection passed 122 tests, including exact-byte trust, runner
verdict rejection, second-file failure, cross-thread name collision, stage-only
approval and rejection, picker and actual CLI dispatch, rollback and remove
against the live tool surface, protected file-tool approvals, and the
interrupted-publication subprocess recovery. Editing a trusted tool causes the
next reload to remove it from the live surface and report `untrusted_modified`.
The canonical lease selection passed 59 tests covering stage-only approval,
reload failure and retry, trust mutation ownership, protected-path approvals,
and the Safe gate for a published execute-capable tool. The workflow return
connection still lands in WFC-01.

## M2 verification

`ed01cf0` added the live skill tools and their worker scope. The scripted
provider test publishes a skill during the first request, then verifies the
inventory, rendered instructions, provenance and remaining tool schemas.
The reload stress test pairs each skill's instructions with its template and
reference through successive generations. The real `create_skills` entry and
its headless scenario remain WFC-09 and WFC-13 work.

The first integration run exposed a test assertion that expected JSON text
for the inventory. The provider carried a list. The first full unit run also
exposed a missing renderer snapshot and an unread publication-policy
setting. `5d514e6` reads the configured policy when constructing the session
publisher and renders the artifact receipt in the scrollback. A later
integration run failed on the subagent panel equality check because one
elapsed duration rounded to 0.1 seconds and the other to 0.0 seconds. The
individual test and an isolated full integration run passed with no change
to the duration check.

The verified M2 gate on Python 3.13 passed:

- 6,260 unit tests with 15 skips, 413 integration tests, and 238 E2E tests
  with two skips.
- Nine performance tests, including the reload lag benchmark.
- Ruff check and format, mypy on 423 files, the 10-metric type audit, strict
  MkDocs, and the public-symbol documentation check.

The logs are `/var/tmp/agenthicc-wfc-m2-*.log`. Windows and combined
line and branch coverage remain CI gates. The whole-repository coverage
floor in CI is 81.5%.

## M3 verification

WFC-01 connected the TUI and headless executor to the session's workflow
stack. The tests exercise the normal base, child phase outputs, a synthetic
return with a stable delivery key, typed failure, resume, abandon, and
selection refusal while the child is active. A paused child retains its
loaded definition through registry removal and refuses a replacement with
an incompatible topology. A headless script completed two child phases
and sent the parent's model exactly one return block. A separate TUI test
received a published skill on the parent's first request.

The first full run found three fixtures that lacked the new session stack,
and an E2E reset assertion that expected an empty selection despite a mode
default. The first-frame probe counted 387 modules against a budget of 386.
The publisher is now constructed when a publication or operator action
first needs it. The probe counted 381 modules after that change. The
provider script was corrected to check phase history in the captured
request. The integration and E2E tests exercise the same runtime paths as
the session commands.

The final checks passed on Python 3.13:

- 6,268 unit tests with 15 skips, 419 integration tests, and 239 E2E tests
  with two skips.
- Nine performance tests, Ruff check and format, mypy on 424 source files,
  the type audit against `origin/main`, strict MkDocs, and `llms_check`.
- Zero legacy workflow-selector references across source, tests and docs.
  The type audit reduced the `getattr` count from 459 to 456.

During the serial recheck, the transcript E2E test observed a collapsed
command while expecting the expanded view. The individual test and the
isolated full E2E suite passed with no source or assertion changes. The
verification logs are `/var/tmp/agenthicc-wfc-m3-*.log`.
