# Workflow composition implementation log

Branch `feat/workflow-composition` starts at `5100c8c` from fetched
`origin/main`. The dedicated worktree is `/root/python_projects/agenthicc-wfc`.

| Step | Status | Commit and evidence |
|---|---|---|
| Setup | Complete | `4deacb1` adds the series and its index block. Parsed all 15 front-matter blocks and resolved the series links. `2b9902a` applies the separate guidance patch. Hooks installed. Strict MkDocs, Ruff and mypy passed. |
| 0 | M0 pushed | `a04bfb4` records three spikes and four amendments. The tool-surface follow-up also demonstrates dispatch after a same-turn schema refresh. See milestone gate results below. |
| 1 | In progress | WFC-14 foundation and WFC-05 landed. WFC-13 harness in progress. |
| 2 | Pending | WFC-11 and WFC-10. |
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
| WFC-11 | Spike complete | Sandboxed import, read-only workspace, network isolation and bounds confirmed. |
| WFC-12 | Pending | — |
| WFC-13 | Pending | — |
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
