# Workflow composition implementation log

Branch `feat/workflow-composition` starts at `5100c8c` from fetched
`origin/main`. The dedicated worktree is `/root/python_projects/agenthicc-wfc`.

| Step | Status | Commit and evidence |
|---|---|---|
| Setup | Complete | `4deacb1` adds the series and its index block. Parsed all 15 front-matter blocks and resolved the series links. `2b9902a` applies the separate guidance patch. Hooks installed. Strict MkDocs, Ruff and mypy passed. |
| 0 | M0 pushed | `a04bfb4` records three spikes and four amendments. The tool-surface follow-up also demonstrates dispatch after a same-turn schema refresh. See milestone gate results below. |
| 1 | M1 pushed | WFC-14 foundation, WFC-05 and the WFC-13 harness landed. Full local gate and pre-push checks passed. The remote tip matched `aa2bac1`. |
| 2 | M2 pushed | WFC-11 publication service and WFC-10 live skills. The remote tip matched `7ca93be`. |
| 3 | M3 pushed | WFC-01 and the publish connection. The remote tip matched `f0eb339`. |
| 4 | M4 pushed | WFC-02 workflow entry and inspection. The remote tip matched `4c5e0cc`. |
| 5 | M5 pushed | WFC-06 authoring, publication, and immediate use. Pre-push checks passed and the remote tip matched `35165f3`. |
| 6 | M6 pushed | WFC-08 and WFC-07 passed the complete local gate and pre-push hooks. The remote tip matched `b24f9dc`. |
| 7 | M7 pushed | WFC-09 and S5 passed the full local gate and push hooks. The remote tip matched `d983ae5`. |
| 8 | M8 pushed | Worker workflows and durable stack recovery. The remote tip matched `5fadb96` after the pre-push checks. |
| 9 | Local gate passed | `2612298` adds catalog and operator controls. The WFC-13 close-out includes S1–S9 and the cross-version matrix. See the final verification below. |

| PRD | Status | Commit and evidence |
|---|---|---|
| WFC-01 | Implemented | `f0eb339`. See the PRD for stack, child resume and return-delivery evidence. |
| WFC-02 | Implemented | `4c5e0cc` adds model entry, pending handoff, and status in the TUI and headless workflow owner. See the PRD for A1–A6 evidence. |
| WFC-03 | Implemented | `5fadb96` adds the stack reducer, checkpoint schema 2, linked resume, and S7 subprocess recovery at three delivery boundaries. |
| WFC-04 | Implemented | `e90a25c` runs workflow workers in isolated worktrees. S2 covers disjoint parallel staging, cancellation, mixed tasks, typed failure, and parent-owned publication. |
| WFC-05 | Implemented | `c757516`, `7bd642c`. Both runners use the service. See the PRD for A1–A7 evidence. |
| WFC-06 | Implemented | `35165f3` adds session-owned publication, exact predecessor checks, and S3. WFC-04 exercises the workflow-worker integration. |
| WFC-07 | Implemented | `b24f9dc` plans and validates the complete tool set, then publishes it before the parent resumes. S4 observes immediate dispatch and atomic failure. |
| WFC-08 | Implemented | `ab27d2d` switches direct requests, phase entry, and worker spawn to the shared tool generation. WFC-07 exercises the author-and-call scenario. |
| WFC-09 | Implemented | `d983ae5` validates the staged skill set with the real loader. S5 covers live model use, the skill picker, alias collision, advisories, and operator rejection. |
| WFC-10 | Implemented | `ed01cf0`. Provider discovery and use share the live skill registry. |
| WFC-11 | Implemented | `6958a4b` isolates validation and smoke. `59e893a` adds publication, operator controls and recovery. `545ea1d` uses the same store for canonical leases. S6–S8 verify trust integrity, interrupted publication, and stage-only approval. |
| WFC-12 | Implemented | `2612298` adds the model and operator catalog, stack, frame notices and reload and authored summaries. See the PRD for A1–A8 evidence. |
| WFC-13 | Implemented | `7536d33` adds the offline harness. The M9 acceptance close-out finishes S1–S9 and adds cross-version integration. See the criterion and mutation evidence below. |
| WFC-14 | Implemented | `4ce58eb` extracts the scoped staging writers, deterministic verdicts, and persisted repair budgets. All three authoring workflows use the contract. Executable candidates go through the isolated loader check. |

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

## M4 verification

The parent closes its provider turn before starting an accepted workflow.
The TUI and headless session also park an opted-in workflow and resume the same
run on return. The scripted S1 scenario exercises model discovery, entry,
two child phases and one return. Entry into the actual `create_tools` authoring
workflow is exercised at WFC-07.

The first full runs found five unit tests and one integration test failing
with `AttributeError: 'Handle' object has no attribute 'pause_reason'` or the
same error on a test handle subclass. The workflow stack now owns the pinned
definition in both runners. Focused resume and approval tests passed, including
a registry reload that removes an active parent while its child runs.

The checks after the source fix passed on Python 3.13:

- 6,278 unit tests with 15 skips, 425 integration tests, and 239 E2E tests
  with two skips.
- Nine performance tests, Ruff check and format, mypy on 425 source files,
  the type audit against `origin/main`, strict MkDocs, and `llms_check`.
- No `Any` annotations in the new implementation or test files. The
  repository coverage floor remains 81.5% and the aggregate coverage run
  is scheduled for CI.

The verification logs are `/var/tmp/agenthicc-wfc-m4-*.log`.

## M5 verification

The real authoring runner leaves the candidate under its pending marker through
summary. The session publishes it and reloads the registry before the model
calls the new workflow. The stage-only cases run the operator commands to
approve and reject. The failure and TUI abandonment cases preserve the live
registry and trust records and discard their pending packages. Authored updates
require the recorded inventory and digest. Startup retires unchanged seeded
copies and reports edited copies.

Five early publication tests returned `trusted_location` because pytest's
ordinary fixture marks its temporary root trusted. The session tests now use a
separate project and operator trust store. One interruption test initially
timed out because the TUI workflow config retained its original transport.
The test now interrupts the actual transport used by the workflow. The
replacement fixtures preserve the production trust and checkpoint guards.

The final checks passed on Python 3.13:

- 6,275 unit tests with 15 skips, 427 integration tests, and 243 E2E tests
  with two skips.
- Nine performance tests, Ruff check and format, mypy on 425 source files,
  the type audit against `origin/main`, strict MkDocs, and `llms_check`.
- The production trust call-site scan and the retired design-tool search.
  The full coverage report remains a CI gate at 81.5%.

The verification logs are `/var/tmp/agenthicc-wfc-m5-*.log`.

## WFC-08 verification

- The focused suite passed 497 tests on Python 3.13. It includes the actual
  provider requests before and after a reload, generation pinning for workers,
  the next phase of the same CodePlan runner, replayed tool names, revoked
  plugin bytes, missing authored capability declarations, and reload latency.
- Ruff check and format, mypy, the type-audit ratchet, strict MkDocs, and
  the public-symbol check passed. The four superseded snapshot names are
  absent from active source and tests.
- WFC-07 supplies the remaining end-to-end author-and-call scenario.

## M6 verification

- WFC-08 is in `ab27d2d`. WFC-07 replaces the single-tool scaffold and its
  seeded skill with the callable `create_tools` workflow. The session
  publishes only the entire validated set and reloads its exact bytes before
  resuming the parent. S4 covers three tools in two modules, a successful
  invocation on the first resumed request, and one failing module exhausting
  the shared repair budget without publication.
- Initial unit failures exposed an outdated schema count and invocation table,
  two noncanonical error codes, a missing queued-input callback, and a
  migration fixture saving a conflicting checkpoint revision. Three cassette
  tests failed because the replay processor lacked `emit_nowait`. The focused
  tests and the cassette replays passed after the fixes.
- The full rerun passed 6,298 unit tests with 15 skips, 431 integration tests,
  245 E2E tests with two skips, and all nine performance tests. Ruff check and format, mypy, the type-audit
  ratchet, strict MkDocs, and the public-symbol check passed. Removing the old
  workflow reduced the mypy module suppression metric from six to five.
- The repository coverage floor is 81.5%. The full coverage measurement is
  the CI gate specified by the implementation prompt.

## M7 verification

- S5 published two skills, reloaded their command entries and inventory, and
  applied the skill's tool restrictions on the next provider request after
  activation. The test uses the existing dollar-trigger picker. An alias
  matching the built-in help command was refused in preview. The stage-only
  case rejected the entire set through the operator command.
- The shared parser and discovery agreed on 200 generated cases. The focused
  prototype passed 156 checks. During prototyping, checking canonical skill
  triggers against slash commands incorrectly refused the existing
  `$release-notes` name. The final collision test checks the actual canonical
  command and both spellings of each alias.
- The full milestone run passed 6,310 unit tests with 15 skips, 431 integration
  tests, and 248 E2E tests with two skips. Ruff check and format, mypy, type
  audit, strict MkDocs, and the public-symbol check passed.
- All nine performance tests passed.

## WFC-04 verification

The focused run passed 519 unit, integration, and E2E tests on Python 3.13.
It covers headless workflow execution, worker dispatch, stack sessions, and
the self-extension scenarios. Ruff check and format, mypy on 438 source files,
the type-audit ratchet, strict MkDocs, and the public-symbol check passed.
The logs are `/var/tmp/agenthicc-wfc-m8-wfc04-*.log`. The full M8 gate follows
durable stack recovery.

## WFC-03 verification

The focused return suite passed 17 checks. It stops the actual session after
the private packet, after the durable kernel return, and after the conversation
journal accepts the parent turn. Each reopened session sends one parent
request. A further reopen sends none. A separate reopened TUI restores a
paused child and its suspended caller before the operator resumes the child.
The publication test interrupts the first of three file moves and verifies
that recovery restores the previous state with all three candidates staged.

The journal-start crash initially reached the provider without its synthetic
message because the generic resume path expected the input to be present.
The workflow return now appends that input under its durable turn key before
provider dispatch. The full unit run also found two CLI fixture failures
reporting `SimpleNamespace` had no `workflow_stack_max_depth`. The fixture
now supplies the setting and kernel state, and all 12 CLI checks passed.

Ruff, mypy, the type audit, strict MkDocs, and the public-symbol check passed
after the final source change. The full M8 rerun is in
`/var/tmp/agenthicc-wfc-m8-rerun-*.log`.

The unit rerun passed 6,325 tests with 15 skips. Integration exposed three
fixture gaps. The background project workflow lacked a bridge, the recovery
kernel stand-in lacked `workflow_frames`, and the continuation handle lacked
`frame_id`. The focused rerun passed four integration and end-to-end cases
after those fixtures were brought up to the runtime contract. The final
milestone gates are in `/var/tmp/agenthicc-wfc-m8-final-*.log`.

The first E2E run imported 388 modules before the first frame, exceeding
the 386-module limit. A fresh TUI now reconciles publication before deferred
extension discovery. Headless and resumed sessions reconcile before eager
discovery. The unchanged first-frame budget and five recovery checks
passed in `/var/tmp/agenthicc-wfc-m8-startup-rerun.log`.

The full final milestone passed 6,325 unit tests with 15 skips, 431 integration
tests, 259 E2E tests with two skips, and all nine performance tests. Ruff, mypy,
type audit, strict MkDocs, and the public-symbol check passed. The pre-push
hook passed and the branch reached `5fadb96`.

## M9 verification

The first catalog prototype left out `release-notes` when the workflow list
used the prompt budget. The catalog now reserves space for matching skills
and new tools. The first integration capture reported `No active workflow`
before the event processor had projected the frame. The test drains the
processor before reading the catalog. The busy-command fixture now uses
`/workflow reset` for mutation and covers bare `/workflow` as inspection.

The first full unit run passed 6,334 tests with 15 skips and five failures.
Four picker tests expected the old results for `/workflow ` and one reported
the extra registered kind `workflow_frame`. The updated picker tests cover
prefix collisions and static actions without a workflow registry. The new
renderer snapshot exposed an omitted notice body. Workflow frame notices now
show the entry intent and return summary at all detail levels.

All 28 S1–S9 tests passed in 67.76 seconds on Python 3.12. S2, S4, S9,
and the first-frame budget passed together in 20.65 seconds. The source
check found zero references to `Any` in added Python modules. The legacy
scan retained the schema-1 rename diagnostic and its test, recorded cassette
history, and the bootstrap retirement identities. Every traceability node
exists, as do all 121 named test references in the PRD status sections.

Three temporary mutations failed their tests as expected:

- Skipping the live generation refresh hid `live_probe` from the next model
  request in `test_direct_turn_uses_new_schema_and_dispatch_after_reload`.
- Dropping the workflow return key caused `Unexpected model request 1` in
  `test_restart_delivers_one_parent_turn_after_durable_return[returned]`.
  The child explicitly imports from the checkout under test.
- Giving a model tool module an `ExtensionTrust` import failed
  `test_model_tool_modules_do_not_reference_extension_trust_mutators`.

The implementation passed Ruff check and format, mypy over 440 source files,
all ten type-audit metrics, strict MkDocs, `llms_check`, and
`typecheck_contracts`. Whole-repository statement and branch coverage will be
measured by the PR CI run with the existing 81.5% floor.

After the renderer fix, the full Python 3.13 rerun passed 6,340 unit tests
with 15 skips, 434 integration tests, 263 E2E tests with two skips, and all
nine performance tests. Logs are in `/var/tmp/agenthicc-wfc-m9-final-*.log`.
Strict MkDocs still reports the existing unlisted question-timeouts guide
and a missing troubleshooting anchor as informational diagnostics.
