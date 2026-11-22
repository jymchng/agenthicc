# Workflow composition implementation log

Branch `feat/workflow-composition` starts at `5100c8c` from fetched
`origin/main`. The dedicated worktree is `/root/python_projects/agenthicc-wfc`.

| Step | Status | Commit and evidence |
|---|---|---|
| Setup | Complete | `4deacb1` adds the series and its index block. Parsed all 15 front-matter blocks and resolved the series links. `2b9902a` applies the separate guidance patch. Hooks installed. Strict MkDocs, Ruff and mypy passed. |
| 0 | Spikes pass | `a04bfb4` records three spikes and four amendments. The tool-surface follow-up also demonstrates dispatch after a same-turn schema refresh. See milestone gate results below. |
| 1 | Pending | WFC-14, WFC-05 and WFC-13 harness. |
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
| WFC-05 | Pending | — |
| WFC-06 | Pending | — |
| WFC-07 | Pending | — |
| WFC-08 | Spike complete | Same-turn refresh verified by updating the provider schema list and the dispatch map together. |
| WFC-09 | Pending | — |
| WFC-10 | Pending | — |
| WFC-11 | Spike complete | Sandboxed import, read-only workspace, network isolation and bounds confirmed. |
| WFC-12 | Pending | — |
| WFC-13 | Pending | — |
| WFC-14 | Pending | — |

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
